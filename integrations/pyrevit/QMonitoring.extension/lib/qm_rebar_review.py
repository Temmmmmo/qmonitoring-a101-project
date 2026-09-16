# -*- coding: utf-8 -*-
"""Whole-batch straight Rebar review contract. Graphic input is never approval."""
from __future__ import division, unicode_literals

import copy
import math

from qm_plate_packet import DIRECTIONS, MASS_PER_MM_PER_DIAMETER_SQUARED
from qm_probe_geometry import distance
from qm_revit_plan_preview import build_preview_primitives, _validate_primitives
from qm_trial_input import exact_keys, number

VERSION = "0.1.3"
REPORT_SCHEMA = "revit-rebar-review-mvp-report/v1"
INPUT_SCHEMAS = ("graphic-bar-plan-draft/v1", "graphic-bar-plan-pruned/v1", "graphic-bar-plan-repaired/v1")
AXIS_TOLERANCE_MM = 0.01
OUTER_COMPUTATIONAL_EPS_MM = 0.000001
MAX_REVIEW_BARS = 5000
AXIS_DEPTH_PROFILE = "mvp-face40-clear4-max-model-diameter/v1"


def material_key(bar):
    return "{0}|{1}".format(bar["steel_class"].strip(), bar["diameter_mm"])


def computed_axis_depth_policy(primitives, host, bar_types):
    """Explicit MVP offsets, independent of native cover and FE layer profiles."""
    diameters = dict((direction, []) for direction in DIRECTIONS)
    for bar in primitives["bars"]:
        typ = bar_types[material_key(bar)]
        for field in ("nominal_diameter_mm", "model_diameter_mm"):
            number(typ[field], 6, 40)
            if abs(typ[field]-bar["diameter_mm"]) > .001:
                raise ValueError("Automatic axis profile requires exact selected nominal/model diameter")
        diameters[bar["direction"]].append(typ["model_diameter_mm"])
    if not any(diameters.values()):
        raise ValueError("Automatic axis profile needs a nonempty whole inventory")
    maximum = dict((d,max(values or [0])) for d,values in diameters.items())
    depths, zs, gaps = {}, {}, {}
    for layer in ("bottom", "top"):
        x, y = layer+"-X", layer+"-Y"
        rx, ry = maximum[x]/2, maximum[y]/2
        depths[x] = 40+rx
        depths[y] = (depths[x]+rx+4+ry if rx else 40+ry) if ry else 40
        for direction in (x,y):
            if maximum[direction]:
                z = host[layer+"_z_mm"]+(depths[direction] if layer == "bottom" else -depths[direction])
                radius = maximum[direction]/2
                if z-radius < host["bottom_z_mm"]-OUTER_COMPUTATIONAL_EPS_MM or z+radius > host["top_z_mm"]+OUTER_COMPUTATIONAL_EPS_MM:
                    raise ValueError("Automatic MVP axis profile does not fit native thickness; select another explicit manual profile")
                zs[direction] = z
        if rx and ry:
            gaps[x+"/"+y] = abs(zs[x]-zs[y])-rx-ry
    for lower in ("bottom-X","bottom-Y"):
        for upper in ("top-X","top-Y"):
            if lower in zs and upper in zs:
                gaps[lower+"/"+upper] = zs[upper]-zs[lower]-(maximum[lower]+maximum[upper])/2
    if any(gap < 4-OUTER_COMPUTATIONAL_EPS_MM for gap in gaps.values()):
        raise ValueError("Automatic MVP axis profile needs at least 4 mm vertical body gap; native thickness is too small")
    return {"schema_version":"revit-axis-depth-policy/v1","profile_id":AXIS_DEPTH_PROFILE,
        "mode":"auto","user_confirmed":False,"face_to_steel_offset_mm":40,
        "minimum_interlayer_clear_mm":4,"native_cover_used":False,"engineering_approval":False,
        "computed_depths_mm":depths,"actual_depths_mm":copy.deepcopy(depths),
        "maximum_model_diameter_mm_by_direction":maximum,
        "inactive_directions":[d for d in DIRECTIONS if not maximum[d]],
        "native_face_z_mm":{"bottom":host["bottom_z_mm"],"top":host["top_z_mm"]},
        "absolute_axis_z_mm_by_direction":zs,"minimum_vertical_body_gap_mm_by_pair":gaps,
        "automatic_gap_policy_applied":True,
        "scope":"MVP face offset 40 and vertical gap 4; NOT native cover, norm, full collision proof or engineering approval"}


def manual_axis_depth_policy(depths, proposal=None, auto_error=None):
    exact_keys(depths,DIRECTIONS)
    for value in depths.values():
        number(value,0,1000000)
    return {"schema_version":"revit-axis-depth-policy/v1","profile_id":None,"mode":"manual",
        "user_confirmed":False,"native_cover_used":False,"engineering_approval":False,
        "actual_depths_mm":copy.deepcopy(depths),
        "computed_depths_mm":copy.deepcopy(proposal["computed_depths_mm"]) if proposal else None,
        "auto_proposal":copy.deepcopy(proposal),"auto_proposal_error":auto_error,
        "automatic_gap_policy_applied":False,
        "scope":"Explicit manual review depths; automatic face40/gap4 policy NOT certified"}


def select_axis_depth_policy(primitives,host,bar_types,choose_mode,ask_manual):
    """Normal path never asks for four numbers; cancellation never creates bars."""
    proposal, error = None, None
    try:
        proposal = computed_axis_depth_policy(primitives,host,bar_types)
    except ValueError as exc:
        error = str(exc)
    mode = choose_mode(proposal,error)
    if mode == "auto" and proposal is not None:
        policy = proposal
    elif mode == "manual":
        text = ask_manual(proposal)
        if text is None:
            raise ValueError("Manual axis depth settings cancelled; no Rebar created")
        values = [float(part.strip().replace(",",".")) for part in text.split(";")]
        if len(values) != 4:
            raise ValueError("Manual depths require bottom X; bottom Y; top X; top Y")
        policy = manual_axis_depth_policy(dict(zip(DIRECTIONS,values)),proposal,error)
    else:
        raise ValueError("Axis depth profile not confirmed; no Rebar created")
    policy["user_confirmed"] = True
    return policy


def validate_axis_depth_policy(primitives,host,bar_types,depths,policy):
    """Recompute automatic profile with CURRENT native faces and selected types."""
    if (policy.get("schema_version") != "revit-axis-depth-policy/v1"
            or policy.get("user_confirmed") is not True or policy.get("native_cover_used") is not False
            or policy.get("engineering_approval") is not False or policy.get("actual_depths_mm") != depths):
        raise ValueError("Exact confirmed axis-depth provenance differs")
    if policy.get("mode") == "auto":
        fresh = computed_axis_depth_policy(primitives,host,bar_types)
        fresh["user_confirmed"] = True
        if policy != fresh:
            raise ValueError("Native host/types or automatic axis profile changed; confirm a fresh proposal")
    elif policy.get("mode") == "manual":
        if policy.get("automatic_gap_policy_applied") is not False or policy.get("profile_id") is not None:
            raise ValueError("Manual depths cannot borrow automatic gap approval")
    else:
        raise ValueError("Unknown axis depth selection mode")
    return copy.deepcopy(policy)


def validated_graphics(packet, offset_x_mm, offset_y_mm):
    if not isinstance(packet, dict) or packet.get("schema_version") not in INPUT_SCHEMAS:
        raise ValueError("Select a complete straight graphic bar plan, not a zone or composite JSON")
    primitives = build_preview_primitives(packet, offset_x_mm, offset_y_mm)
    _validate_primitives(primitives)
    bars = primitives["bars"]
    if not 1 <= len(bars) <= MAX_REVIEW_BARS:
        raise ValueError("Whole review batch exceeds the explicit 5000-bar limit; nothing is sampled")
    if {bar["direction"] for bar in bars} != set(DIRECTIONS):
        # Empty directions are allowed in the packet, but the validator above
        # retains all four inventories. Do not fabricate a bar for an empty one.
        if not set(bar["direction"] for bar in bars).issubset(set(DIRECTIONS)):
            raise ValueError("Unexpected direction in complete graphic plan")
    if primitives["placement_eligible"] is not False or primitives["engineering_approval"] is not False:
        raise ValueError("Review cannot inherit a placement or approval certificate")
    if primitives["summary"]["physical_bar_count"] != len(bars):
        raise ValueError("The entire straight party must be present")
    return primitives


def _outer_loop(face):
    loops = face.get("edge_loops")
    if not isinstance(loops, list) or not loops:
        raise ValueError("Native planar face has no classified line loops")
    candidates = []
    for edges in loops:
        if not isinstance(edges, list) or len(edges) < 3:
            raise ValueError("Short or missing native face loop")
        segments = []
        for edge in edges:
            if edge.get("kind") != "Line":
                raise ValueError("Curved native outer/void loop unsupported in flat MVP; no bbox fallback")
            a, b = edge["start_mm"], edge["end_mm"]
            if len(a) != 3 or len(b) != 3 or any(math.isnan(v) or math.isinf(v) for v in a+b):
                raise ValueError("Nonfinite native host edge")
            if distance(a, b) < AXIS_TOLERANCE_MM:
                raise ValueError("Short native host edge unsupported")
            segments.append((a, b))
        first, previous_end = segments.pop(0)
        points = [first]
        while segments:
            matches = []
            for index, (a, b) in enumerate(segments):
                if distance(previous_end, a) <= AXIS_TOLERANCE_MM:
                    matches.append((index, a, b))
                if distance(previous_end, b) <= AXIS_TOLERANCE_MM:
                    matches.append((index, b, a))
            if len(matches) != 1:
                raise ValueError("Native edge loop has no unique endpoint continuation")
            index, a, b = matches[0]
            segments.pop(index)
            points.append(a)
            previous_end = b
        if distance(previous_end, points[0]) > AXIS_TOLERANCE_MM:
            raise ValueError("Native face edge loop is not closed")
        polygon = [[p[0], p[1]] for p in points]
        area = abs(sum(polygon[i][0]*polygon[(i+1)%len(polygon)][1]
            - polygon[(i+1)%len(polygon)][0]*polygon[i][1] for i in range(len(polygon))))/2
        if area <= AXIS_TOLERANCE_MM:
            raise ValueError("Degenerate native face loop")
        candidates.append((area, polygon))
    candidates.sort(key=lambda row: row[0], reverse=True)
    if len(candidates) > 1 and abs(candidates[0][0]-candidates[1][0]) <= AXIS_TOLERANCE_MM:
        raise ValueError("Disconnected or ambiguous native outer face loops unsupported")
    return candidates[0][1], len(candidates)-1


def flat_outer_host(floor):
    """Classify selected native Floor face loops, not its model bounding box."""
    faces = {}
    for side, normal in (("top", 1), ("bottom", -1)):
        rows = floor.get(side+"_faces")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("Flat MVP needs exactly one native planar top and bottom face")
        face = rows[0]
        plane = face.get("plane")
        if plane is None or len(plane.get("normal", [])) != 3 or len(plane.get("origin_mm", [])) != 3:
            raise ValueError("Sloping or nonplanar native Floor unsupported")
        if any(math.isnan(v) or math.isinf(v) for v in plane["normal"]+plane["origin_mm"]):
            raise ValueError("Nonfinite native Floor plane")
        if any(abs(a-b) > 1e-8 for a,b in zip(plane["normal"], (0,0,normal))):
            raise ValueError("Sloping native Floor unsupported")
        polygon, holes = _outer_loop(face)
        if any(abs(point[2]-plane["origin_mm"][2]) > AXIS_TOLERANCE_MM
                for edges in face["edge_loops"] for edge in edges for point in (edge["start_mm"],edge["end_mm"])):
            raise ValueError("Native boundary edges differ from flat face elevation")
        faces[side] = {"z_mm": plane["origin_mm"][2], "outer_xy_mm": polygon, "excluded_hole_count": holes}
    if faces["top"]["z_mm"]-faces["bottom"]["z_mm"] <= AXIS_TOLERANCE_MM:
        raise ValueError("Native Floor thickness is invalid")
    top, bottom = faces["top"]["outer_xy_mm"], faces["bottom"]["outer_xy_mm"]
    # Each native boundary is retained, including its genuine recesses. Vertex
    # counts need not match: Revit can split collinear sides differently.
    # Unequal boundaries are NOT relabeled as a prismatic Solid proof. The
    # K09's flat 200 mm top-subset-bottom geometry uses conservative BOTH checks.
    same = _boundaries_equivalent(top, bottom)
    if not same and (abs(faces["top"]["z_mm"]-faces["bottom"]["z_mm"]-200) > AXIS_TOLERANCE_MM or not _polygon_subset(top, bottom)):
        raise ValueError("Unequal native outer contours unsupported except flat 200 mm TOP-subset-BOTTOM dual-exterior MVP")
    return {"host_id": floor["element_id"], "top_z_mm": faces["top"]["z_mm"],
        "bottom_z_mm": faces["bottom"]["z_mm"], "thickness_mm": faces["top"]["z_mm"]-faces["bottom"]["z_mm"],
        "outer_xy_mm": top, "top_outer_xy_mm": top, "bottom_outer_xy_mm": bottom,
        "outer_boundaries_equivalent": same,
        "outer_computational_epsilon_mm": OUTER_COMPUTATIONAL_EPS_MM,
        "outer_profile": "equivalent-flat-exteriors/v1" if same else "flat200-top-subset-bottom-dual-exterior-mvp/v1",
        "outer_policy": "every bar body inside BOTH native top and bottom outer projections; no bbox; intermediate Solid not certified",
        "excluded_hole_count": max(faces["top"]["excluded_hole_count"], faces["bottom"]["excluded_hole_count"]),
        "cover_metadata": copy.deepcopy(floor.get("covers")),
        "scope": "native flat Floor planar outer line loops + thickness; holes and cover excluded; NOT actual-Solid containment proof"}


def _boundaries_equivalent(first, second):
    """Subdivision/order-independent line-boundary identity, strict 0.01 mm."""
    def segment_covered(a, b, polygon):
        length = distance(a, b)
        intervals = []
        for index, c in enumerate(polygon):
            d = polygon[(index+1)%len(polygon)]
            def projection(p):
                return ((p[0]-a[0])*(b[0]-a[0])+(p[1]-a[1])*(b[1]-a[1]))/length
            def off(p):
                return abs((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))/length
            if max(off(c), off(d)) <= AXIS_TOLERANCE_MM:
                lo, hi = sorted((projection(c), projection(d)))
                if hi >= 0 and lo <= length:
                    intervals.append((max(0,lo), min(length,hi)))
        reached = 0.0
        for lo, hi in sorted(intervals):
            if lo > reached+AXIS_TOLERANCE_MM:
                return False
            reached = max(reached, hi)
        return reached >= length-AXIS_TOLERANCE_MM
    return all(segment_covered(polygon[i], polygon[(i+1)%len(polygon)], other)
        for polygon, other in ((first,second),(second,first)) for i in range(len(polygon)))


def _polygon_subset(inner, outer):
    """Check every edge subinterval, including crossings at outer vertices."""
    if any(not _point_inside(p,outer) for p in inner):
        return False
    for index,a in enumerate(inner):
        b = inner[(index+1)%len(inner)]
        vector = [b[i]-a[i] for i in range(2)]
        squared = sum(v*v for v in vector)
        cuts = [0.0,1.0]
        for j,c in enumerate(outer):
            d = outer[(j+1)%len(outer)]
            edge = [d[i]-c[i] for i in range(2)]
            delta = [c[i]-a[i] for i in range(2)]
            determinant = vector[0]*edge[1]-vector[1]*edge[0]
            if abs(determinant) > 1e-9:
                t = (delta[0]*edge[1]-delta[1]*edge[0])/determinant
                u = (delta[0]*vector[1]-delta[1]*vector[0])/determinant
                if 0 <= t <= 1 and -1e-9 <= u <= 1+1e-9:
                    cuts.append(t)
            else:
                for p in (c,d):
                    t = sum((p[i]-a[i])*vector[i] for i in range(2))/squared
                    if 0 <= t <= 1 and distance(p,[a[i]+t*vector[i] for i in range(2)]) <= AXIS_TOLERANCE_MM:
                        cuts.append(t)
        cuts.sort()
        for lo,hi in zip(cuts,cuts[1:]):
            t = (lo+hi)/2
            if not _point_inside([a[i]+t*vector[i] for i in range(2)],outer):
                return False
    return True


def _point_inside(point, polygon):
    x,y = point
    inside = False
    for index, a in enumerate(polygon):
        b = polygon[(index+1)%len(polygon)]
        cross = (b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0])
        if (abs(cross)/distance(a,b) <= OUTER_COMPUTATIONAL_EPS_MM
                and min(a[0],b[0])-OUTER_COMPUTATIONAL_EPS_MM <= x <= max(a[0],b[0])+OUTER_COMPUTATIONAL_EPS_MM
                and min(a[1],b[1])-OUTER_COMPUTATIONAL_EPS_MM <= y <= max(a[1],b[1])+OUTER_COMPUTATIONAL_EPS_MM):
            return True
        if (a[1] > y) != (b[1] > y) and x < a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1]):
            inside = not inside
    return inside


def _proper_cross(a,b,c,d):
    def orient(p,q,r):
        length = distance(p,q)
        return ((q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]))/length if length else 0.0
    def opposite(first,second):
        return ((first > OUTER_COMPUTATIONAL_EPS_MM and second < -OUTER_COMPUTATIONAL_EPS_MM)
            or (second > OUTER_COMPUTATIONAL_EPS_MM and first < -OUTER_COMPUTATIONAL_EPS_MM))
    return opposite(orient(a,b,c),orient(a,b,d)) and opposite(orient(c,d,a),orient(c,d,b))


def _corridor_contained(start,end,radius,polygon):
    if abs(start[0]-end[0]) <= AXIS_TOLERANCE_MM:
        corners = [[start[0]-radius,start[1]], [start[0]+radius,start[1]],
            [end[0]+radius,end[1]], [end[0]-radius,end[1]]]
    else:
        corners = [[start[0],start[1]-radius], [end[0],end[1]-radius],
            [end[0],end[1]+radius], [start[0],start[1]+radius]]
    if any(not _point_inside(corner,polygon) for corner in corners):
        return False
    # A narrow concave bay may meet a corridor edge exactly at a native vertex
    # (proper_cross excludes endpoint contact). Clip every polygon edge to the
    # body rectangle and reject any positive boundary segment in its interior.
    low = [min(p[i] for p in corners) for i in range(2)]
    high = [max(p[i] for p in corners) for i in range(2)]
    for index, a in enumerate(polygon):
        b = polygon[(index+1)%len(polygon)]
        enter, leave = 0.0, 1.0
        for axis in range(2):
            delta = b[axis]-a[axis]
            if abs(delta) < 1e-12:
                if a[axis] < low[axis] or a[axis] > high[axis]:
                    leave = -1
                    break
            else:
                t1, t2 = (low[axis]-a[axis])/delta, (high[axis]-a[axis])/delta
                enter, leave = max(enter,min(t1,t2)), min(leave,max(t1,t2))
        if leave > enter:
            t = (enter+leave)/2
            midpoint = [a[i]+t*(b[i]-a[i]) for i in range(2)]
            if all(low[i]+OUTER_COMPUTATIONAL_EPS_MM < midpoint[i] < high[i]-OUTER_COMPUTATIONAL_EPS_MM for i in range(2)):
                return False
    for i,a in enumerate(corners):
        b = corners[(i+1)%4]
        midpoint = [(a[0]+b[0])/2,(a[1]+b[1])/2]
        if not _point_inside(midpoint,polygon):
            return False
        for j,c in enumerate(polygon):
            d = polygon[(j+1)%len(polygon)]
            if _proper_cross(a,b,c,d):
                return False
    return True


def make_review_plan(primitives, host, bar_types, depths):
    exact_keys(depths, DIRECTIONS)
    for value in depths.values():
        number(value, 0, 10000)
    selected, runs, mass = {}, [], []
    for bar in primitives["bars"]:
        key = material_key(bar)
        if key not in bar_types:
            raise ValueError("No explicitly selected exact bar type for "+key)
        actual = bar_types[key]
        for field in ("nominal_diameter_mm", "model_diameter_mm"):
            if abs(actual[field]-bar["diameter_mm"]) > 0.001:
                raise ValueError("Selected RebarBarType diameter differs for "+key)
        selected[key] = actual["element_id"]
        layer, axis = bar["direction"].split("-")
        radius = actual["model_diameter_mm"]/2
        z = host[layer+"_z_mm"] + (depths[bar["direction"]] if layer == "bottom" else -depths[bar["direction"]])
        if z-radius < host["bottom_z_mm"]-AXIS_TOLERANCE_MM or z+radius > host["top_z_mm"]+AXIS_TOLERANCE_MM:
            raise ValueError("Whole batch blocked: bar body outside selected native Floor thickness")
        for side in ("top", "bottom"):
            polygon = host.get(side+"_outer_xy_mm",host["outer_xy_mm"])
            if not _corridor_contained(bar["start_xy_mm"],bar["end_xy_mm"],radius,polygon):
                raise ValueError("Whole batch blocked: {0}/{1} body exceeds native {2} OUTER Floor contour; no clipping or omission".format(
                    bar["direction"],bar["bar_id"],side))
        start,end = bar["start_xy_mm"]+[z],bar["end_xy_mm"]+[z]
        length = distance(start,end)
        if length <= AXIS_TOLERANCE_MM:
            raise ValueError("Zero-length straight bar is unsupported")
        mass.append(MASS_PER_MM_PER_DIAMETER_SQUARED*bar["diameter_mm"]**2*length)
        runs.append({"direction":bar["direction"], "bar_id":bar["bar_id"], "material_key":key,
            "diameter_mm":bar["diameter_mm"], "steel_class":bar["steel_class"], "bar_type_id":actual["element_id"],
            "host_id":host["host_id"], "normal":[0,1,0] if axis == "X" else [1,0,0],
            "axes":[{"start_mm":start,"end_mm":end}], "bar_count":1, "spacing_mm":100,
            "layout_rule":"Single", "allow_new_shape":False, "length_mm":length,
            "source_refs":copy.deepcopy(bar["source_refs"])})
    if len(runs) != primitives["summary"]["physical_bar_count"]:
        raise ValueError("Whole graphic inventory was not carried into native plan")
    expected_mass = primitives["summary"]["additional_mass_kg"]
    if abs(math.fsum(mass)-expected_mass) > max(0.001,expected_mass*1e-9):
        raise ValueError("Native plan length/mass differs from the full graphic party")
    return {"runs":runs, "host":copy.deepcopy(host), "expected":copy.deepcopy(primitives["summary"]),
        "material_selection":selected, "axis_depths_mm":copy.deepcopy(depths),
        "mass_formula":"derived-from-final-centerline:0.000006165*d_mm^2*L_mm; not Revit material density",
        "tolerance_mm":AXIS_TOLERANCE_MM}


def compare_native(plan, rows):
    if not isinstance(rows,list) or len(rows) != len(plan["runs"]):
        raise ValueError("Post-Commit native Rebar count differs from full source inventory")
    issues, masses, seen = [], [], set()
    for wanted,actual in zip(plan["runs"],rows):
        errors = []
        if actual.get("element_id") in seen:
            errors.append("duplicate_element_id")
        seen.add(actual.get("element_id"))
        for key,value in (("host_id",wanted["host_id"]),("quantity",1),("number_of_bar_positions",1),
                ("layout_rule","Single"),("hook_type_ids",[-1,-1]),
                ("review_bar_id",wanted["bar_id"]),("review_direction",wanted["direction"])):
            if actual.get(key) != value:
                errors.append(key)
        typ = actual.get("bar_type",{})
        if typ.get("element_id") != wanted["bar_type_id"]:
            errors.append("bar_type_id")
        if any(abs(typ.get(key,-1)-wanted["diameter_mm"]) > 0.001 for key in ("nominal_diameter_mm","model_diameter_mm")):
            errors.append("diameter_mm")
        bars = actual.get("bars")
        if not isinstance(bars,list) or len(bars) != 1 or bars[0].get("position_index") != 0 or len(bars[0].get("curves",[])) != 1:
            errors.append("single_final_curve")
        elif bars[0]["curves"][0].get("kind") != "Line":
            errors.append("native_straight_shape")
        else:
            curve = bars[0]["curves"][0]
            a,b = curve["start_mm"],curve["end_mm"]
            target = wanted["axes"][0]
            direct = (distance(a,target["start_mm"]),distance(b,target["end_mm"]))
            reverse = (distance(a,target["end_mm"]),distance(b,target["start_mm"]))
            matched = direct if sum(direct) <= sum(reverse) else reverse
            if any(delta > AXIS_TOLERANCE_MM for delta in matched):
                errors.append("absolute_native_axis_xyz")
            length = distance(a,b)
            if abs(curve["length_mm"]-length) > AXIS_TOLERANCE_MM or abs(length-wanted["length_mm"]) > AXIS_TOLERANCE_MM:
                errors.append("native_length_mm")
            masses.append(MASS_PER_MM_PER_DIAMETER_SQUARED*wanted["diameter_mm"]**2*length)
        if errors:
            issues.append({"direction":wanted["direction"],"bar_id":wanted["bar_id"],"checks":errors})
    mass = math.fsum(masses)
    if len(masses) != len(plan["runs"]) or abs(mass-plan["expected"]["additional_mass_kg"]) > 0.01:
        issues.append({"checks":["complete_physical_count_or_derived_mass"]})
    return {"status":"matches" if not issues else "differs", "issues":issues,
        "physical_bar_count":len(masses), "mass_from_final_axes_kg":mass,
        "tolerance_mm":AXIS_TOLERANCE_MM,
        "mass_formula":plan["mass_formula"]}
