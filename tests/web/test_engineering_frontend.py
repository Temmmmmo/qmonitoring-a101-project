"""Frontend contracts and JS behavior with synthetic DOM doubles, not Revit checks."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src/rebar/web/static"


def test_actual_s1_repaired_report_keeps_current_gates_and_export_visible():
    report = Path(__file__).resolve().parents[2] / "artifacts/s1-flat-repaired-report.json"
    if not report.exists():
        pytest.skip("Local revalidated S1 report not available")
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 callbacks:{},addEventListener(name,fn){this.callbacks[name]=fn;},querySelectorAll(){return[];},setAttribute(){}});
const q=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.payload=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
vm.runInContext('result=payload;renderPoint();download=(value,filename)=>{window.downloaded={value,filename};}',context);
assert.equal(q('#download-selected').disabled,false);q('#download-selected').callbacks.click();
assert.equal(context.window.downloaded.value.schema_version,'graphic-bar-plan-repaired/v1');
assert.equal(context.window.downloaded.filename,'graphic-bar-plan-repaired.json');
assert(q('#gate-status').textContent.includes('Нет полной инженерной проверки'));
assert.equal(q('#gate-status').dataset.status,'fail');
const cards=q('#check-summary').innerHTML;
assert(cards.includes('545 КЭ'));assert(cards.includes('1383')||cards.includes('1 383'));
assert(cards.includes('Исходный спрос сохранён'));assert(!cards.includes('38 КЭ'));
assert(q('#drawing-view-note').textContent.includes('параметрические зоны до физической обработки'));
assert.equal(q('#drawing').dataset.view,'source');
"""
    node(script, STATIC / "composite.js", report)


class Page(HTMLParser):
    def __init__(self, name):
        super().__init__()
        self.ids, self.details, self.stack = {}, {}, []
        self.feed((STATIC / name).read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            assert attrs["id"] not in self.ids, f"Duplicate ID {attrs['id']}"
            self.ids[attrs["id"]] = (tag, attrs)
        if tag == "details":
            self.stack.append(attrs.get("id", attrs.get("class")))
        if attrs.get("id") in ("blockers", "check-summary"):
            self.details[attrs["id"]] = list(self.stack)

    def handle_endtag(self, tag):
        if tag == "details":
            assert self.stack
            self.stack.pop()


def test_start_is_real_one_click_and_old_workspace_remains_available():
    page = Page("index.html")
    assert page.ids["run-engineering-example"][0] == "button"
    assert "disabled" in page.ids["run-engineering-example"][1]
    assert page.ids["advanced"][0] == "details" and "open" not in page.ids["advanced"][1]
    for key in ("analysis-form", "demo-select", "algorithm-list", "results", "example-files"):
        assert key in page.ids
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "Рассчитать плиту" in html
    assert 'id="example-selector"' in html
    assert html.index('id="run-engineering-example"') < html.index('id="analysis-form"')
    assert 'href="/composite#custom-inputs"' in html


def test_composite_keeps_all_controls_but_checks_are_visible_and_metrics_unambiguous():
    page = Page("composite.html")
    for key in ("composite-form", "run", "run-demo", "run-engineering-example", "example-files",
            "metrics", "drawing", "candidate", "stock-status", "balance-status", "stock-patterns",
            "direction-tabs", "installation-notes", "schedule", "blockers", "download-report", "download-selected",
            "check-summary", "engineer-comparison", "comparison-rows", "drawing-layers", "zoom-reset",
            "drawing-views", "source-zone-rows", "source-legend", "download-source", "metrics-scope",
            "boundary-trim-form", "boundary-trim-host", "run-boundary-trim", "gate-status", "handoff-note"):
        assert key in page.ids
    assert "source-zone-check-status" in page.ids
    assert page.details == {"check-summary": ["physical-checks"], "blockers": ["physical-checks", "result-details"]}
    assert page.ids["physical-checks"][0] == "details" and "open" not in page.ids["physical-checks"][1]
    assert "open" not in page.ids["custom-inputs"][1]
    html = (STATIC / "composite.html").read_text(encoding="utf-8")
    assert "Синтетический пример" in html and "Расчётный черновик" in html
    assert html.index('id="output"') < html.index('id="run-demo"')
    assert not page.stack
    assert page.ids["drawing"][1]["data-view"] == "source"
    assert "Зоны + стержни" in html and "Отдельный пересчёт К09" in html
    assert "Исходные изополя + зоны" in html and "Физические стержни" in html
    assert html.index('data-view="source"') < html.index('data-view="combined"')
    assert "Перенести зоны в Revit" in html and "SourceWorkflow" in html
    assert "исходные зоны на схеме существуют до физической обрезки" in html
    assert "open" not in page.ids["source-zone-details"][1]
    js = (STATIC / "composite.js").read_text(encoding="utf-8")
    assert "Нет полной инженерной проверки" in js
    assert 'id="source-envelopes" type="checkbox" checked' in html
    assert "const sourceCheck = hasSelectedSourceGraphics() ? result.source_zone_checks : null;" in js
    assert "const edgeCheck = hasSelectedSourceGraphics() ? result.source_zone_edge_checks : null;" in js
    assert "Краевой запас 80d" in js and "40d с каждого конца — отдельный контроль" in js
    assert "непроверенных направлений" in js and "DXF-контур, не модель Revit" in js
    assert "полное исходное покрытие подтверждено в принятой модели" in js
    assert "фактическая плита Revit не проверена" in js
    assert "не сертифицирована для выбранного варианта" in js
    assert "required" in page.ids["boundary-trim-host"][1]


def node(script, *args):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node unavailable; browser smoke is separate")
    completed = subprocess.run([executable, "-e", script, *map(str, args)],
        text=True, capture_output=True, check=False, timeout=10)
    assert completed.returncode == 0, completed.stderr
    return completed


def test_variant_switch_changes_geometry_checks_source_and_revit_download_together():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 callbacks:{},addEventListener(name,fn){this.callbacks[name]=fn;},querySelectorAll(){return[];},setAttribute(){}});
const q=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
function report(id,mass,missing){
 const candidate={svg:`<svg>${id}</svg>`,overlay_svg:`<svg>${id}</svg>`,coverage:{uncovered_cell_count:missing},geometric_presence:{uncovered_cell_count:missing},zone_drafts:[],installation_notes:[]};
 const point={additional_mass_kg:mass,physical_bar_count:12,position_count:4,zone_count:3,bar_schedule:[],
 direction_candidate_indexes:[0,0,0,0],stock_cutting:{status:'fail',groups:[]}};
 return {schema_version:'composite-plate-analysis/v1',selected_index:0,output_kind:'boundary-trimmed-physical-bars',front:[point],warning:id,
 directions:Array.from({length:4},()=>({candidates:[candidate],source_zone_drafts:[],source_svg:`<svg>source-${id}</svg>`})),blocking_check_ids:[],
 source_graphics:{id,directions:Array.from({length:4},()=>({zone_drafts:[],legend:[]}))},source_graphics_candidate_index:0,
 graphic_bar_plan_draft:{schema_version:'graphic-bar-plan-draft/v1',placement_eligible:false,id},
 mvp_checks:{outer_boundary:'pass',original_demand_presence:missing?'fail':'pass',control_40d:'fail',stock_11700:'fail'},
 boundary_trim:{external_boundary_failures_after:0,geometric_presence:{status:missing?'fail':'pass',uncovered_cell_count:missing},
 coverage_with_control_40d:{status:'fail',uncovered_cell_count:missing+10},collisions:{proven_collision_pair_count:0,uncertain_pair_count:0},stock_cutting:{status:'fail'}}};
}
const first=report('first',100,1),second=report('second',200,7);
first.layout_variants=[{label:'mass',metrics:first.front[0],report:null},{label:'count',metrics:second.front[0],report:second}];
context.payload=first;vm.runInContext('setLayoutVariants(payload);renderPoint();download=(value,filename)=>{window.downloaded={value,filename};}',context);
assert(q('#drawing').innerHTML.includes('first'));
q('#candidate').value='1';q('#candidate').callbacks.change();
assert(q('#drawing').innerHTML.includes('second'));assert(q('#metrics').innerHTML.includes('200'));
assert(q('#result-summary').textContent.includes('7 КЭ'));assert.equal(q('#full-result-warning').textContent,'second');
assert.equal(q('#download-source').disabled,false);q('#download-source').callbacks.click();assert.equal(context.window.downloaded.value.id,'second');
q('#download-selected').callbacks.click();assert.equal(context.window.downloaded.value.id,'second');
q('#candidate').value='0';q('#candidate').callbacks.change();assert(q('#drawing').innerHTML.includes('first'));
context.payload=report('ordinary',300,0);vm.runInContext('setLayoutVariants(payload)',context);
assert.equal(vm.runInContext('layoutVariants.length',context),0);
"""
    node(script, STATIC / "composite.js")


def test_project_parameters_are_explained_and_optional_search_is_collapsed():
    html = (STATIC / 'composite.html').read_text(encoding='utf-8')
    assert 'id="layout-help"' in html and 'сам фон не создаёт и не заменяет' in html
    assert 'Для готовой плиты ничего про фон вводить не нужно' in html
    assert 'Добавка @300 со сдвигом 100 проходит через 100, 400, 700 мм' in html
    assert 'Это положение в плане, не границы плиты и не глубина по высоте' in html
    assert 'Настройки поиска и раскроя · можно оставить стандартные' in html
    assert 'Бюджет MILP' not in html
    assert 'id="error-details"' in html and 'id="error-technical"' in html


def test_calculation_error_has_plain_next_step_and_keeps_exact_technical_reason():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 addEventListener(){},querySelectorAll(){return[];},setAttribute(){}});
const q=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
const technical='origin_mm is missing <img src=x onerror=alert(1)>';
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 fetch:async()=>({ok:false,status:422,json:async()=>({detail:technical})}),
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
assert(vm.runInContext('readableCalculationError({httpStatus:409,message:"busy"})',context).includes('Дождитесь'));
assert(vm.runInContext('readableCalculationError({httpStatus:503,message:"files missing"})',context).includes('на сервере'));
const scaleError=vm.runInContext('readableCalculationError({message:"legend mismatch"})',context);
assert(scaleError.includes('legend mismatch'));
assert(vm.runInContext('readableCalculationError({message:"coverage failed"})',context).includes('потребность не уменьшена'));
vm.runInContext('runAnalysis("/api/engineering-examples/k09/analyze",{method:"POST"})',context).then(()=>{
 assert(q('#error').textContent.includes('фоновой сетке'));assert(!q('#error').textContent.includes('<img'));
 assert.equal(q('#error-technical').textContent,technical);assert.equal(q('#error-technical').innerHTML,'');
 assert.equal(q('#error-details').hidden,false);assert.equal(q('#output').hidden,true);
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
    node(script, STATIC / 'composite.js')


def test_empty_server_response_reports_http_status_instead_of_json_parse_error():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 addEventListener(){},querySelectorAll(){return[];},setAttribute(){}});
const q=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 fetch:async()=>({ok:false,status:502,text:async()=>''}),URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext('runAnalysis("/api/analyze-composite-plate",{method:"POST"})',context).then(()=>{
 assert(q('#error').textContent.includes('HTTP 502'));
 assert(!q('#error-technical').textContent.includes('Unexpected end of JSON input'));
 assert.equal(q('#error-details').hidden,false);
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
    node(script, STATIC / 'composite.js')


@pytest.mark.parametrize("file", ["engineering-example.js", "home.js", "composite.js"])
def test_javascript_syntax(file):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node unavailable")
    subprocess.run([executable, "--check", str(STATIC / file)], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("state", ["available", "unavailable", "synthetic", "missing", "bad-id", "api-error"])
def test_catalog_start_uses_actual_files_and_never_synthetic_fallback(state):
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
class Element {constructor(){this.textContent='';this.disabled=true;this.children=[];}append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}addEventListener(){}}
const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const example={id:'k09-typical-3-14',is_available:true,source_kind:'real_engineering_files',title:'Real plate',
profile:{engineering_approval:false,note:'Chosen research phases, not approved'},
reference:{mass_kg:100,physical_bar_count:30,position_count:4,scope:'Full engineer scope',note:'Not an approval'},
sources:[{direction:{layer:'bottom',axis:'X'},dxf_filename:'Настоящая нижняя.dxf',shk_filename:null,mapping_label:'Проверенная шкала'}]};
const state=process.argv[2];if(state==='unavailable'){example.is_available=false;example.status='Actual source checksum mismatch';}if(state==='synthetic')example.source_kind='synthetic';if(state==='bad-id')example.id='../secret';
const context={window:{location:{search:''}},document:{getElementById:get,createElement:()=>new Element(),querySelector:()=>null},URLSearchParams,Intl,
fetch:async url=>{assert.equal(url,'/api/engineering-examples');return {ok:state!=='api-error',json:async()=>({examples:state==='missing'?[]:[example]})};}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.window.engineeringExampleReady.then(value=>{assert.equal(get('run-engineering-example').disabled,state!=='available');
 if(state==='available'){assert.equal(value.id,example.id);assert(get('example-reference-note').textContent.includes('Full engineer scope'));assert(get('example-profile-note').textContent.includes('Chosen research phases'));
 const row=get('example-files').children[0];assert.equal(row.children[1].textContent,'Настоящая нижняя.dxf');assert.equal(row.children[2].textContent,'Проверенная шкала');}
 else {assert.equal(value,null);if(state==='unavailable')assert(get('example-status').textContent.includes('Actual source checksum mismatch'));}
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
    node(script, STATIC / "engineering-example.js", state)


def test_default_s1_unavailable_keeps_available_k09_visible_without_auto_substitution():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {constructor(){this.textContent='';this.disabled=true;this.children=[];}replaceChildren(...x){this.children=x;}addEventListener(){}append(...x){this.children.push(...x);}}
const items=new Map();const get=id=>{if(!items.has(id))items.set(id,new Element());return items.get(id);};
const s1={id:'legacy-s1-t800',title:'С1',is_available:false,source_kind:'real_engineering_files',
 status:'S1 sources not installed',reference:{mass_kg:null},sources:[]};
const k09={id:'k09-typical-3-14',title:'К09',is_available:true,source_kind:'real_engineering_files',sources:[]};
const context={window:{location:{search:''}},document:{getElementById:get,createElement:()=>new Element(),querySelector:()=>null},
 URLSearchParams,Intl,fetch:async()=>({ok:true,json:async()=>({default_example_id:s1.id,examples:[k09,s1]})})};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.window.engineeringExampleReady.then(value=>{
 assert.equal(value,null);assert.equal(get('run-engineering-example').disabled,true);
 assert.equal(get('example-selector').children.length,2);
 assert.equal(get('example-selector').children[1].selected,true);
 assert(get('example-status').textContent.includes('Доступен К09'));
 assert(get('example-reference-note').textContent.includes('нет отдельного сопоставимого эталона'));
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
    node(script, STATIC / "engineering-example.js")


def test_s1_mvp_cards_keep_presence_40d_and_stock_distinct_from_outer_pass():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 addEventListener(){},querySelectorAll(){return[];},setAttribute(){}});
const q=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const candidate={svg:'<svg/>',coverage:{uncovered_cell_count:0},geometric_presence:{uncovered_cell_count:0},
 zone_drafts:[],installation_notes:[]};
const point={additional_mass_kg:99,physical_bar_count:12,position_count:4,zone_count:3,bar_schedule:[],
 direction_candidate_indexes:[0,0,0,0],stock_cutting:{status:'fail',groups:[]}};
context.payload={output_kind:'boundary-trimmed-physical-bars',front:[point],
 directions:Array.from({length:4},()=>({candidates:[candidate],source_zone_drafts:[]})),blocking_check_ids:[],
 engineering_example:{reference:{mass_kg:null,physical_bar_count:null,position_count:null}},
 mvp_checks:{outer_boundary:'pass',original_demand_presence:'fail',control_40d:'fail',stock_11700:'fail',
 openings:'out_of_scope',cover:'out_of_scope',actual_Revit_geometry:'not_checked'},
 boundary_trim:{external_boundary_failures_after:0,geometric_presence:{status:'fail',uncovered_cell_count:38},
 coverage_with_control_40d:{status:'fail',uncovered_cell_count:552},collisions:{proven_collision_pair_count:2,uncertain_pair_count:0},
 stock_cutting:{status:'fail'},actual_Revit_host_informational_failures:undefined}};
vm.runInContext('result=payload;renderPoint()',context);
const cards=q('#check-summary').innerHTML;
const found=[cards.includes('Внешний контур плиты'),cards.includes('0 стержней'),
 cards.includes('38 КЭ'),cards.includes('Исходный спрос сохранён'),cards.includes('552 КЭ'),
 cards.includes('Раскрой 11,7 м'),cards.includes('2 пересечений'),cards.includes('Не факт Revit')];
if(!found.every(Boolean))throw new Error('checks:'+found.join(','));
assert(q('#gate-status').textContent.includes('Нет полной инженерной проверки'));
assert.equal(q('#gate-status').dataset.status,'fail');
assert.equal(q('#engineer-comparison').hidden,true);
assert(q('#mvp-scope').textContent.includes('отверстия, перепады и cover'));
"""
    node(script, STATIC / "composite.js")


def test_result_draw_modes_separate_metrics_comparison_and_unknown_checks():
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const elements=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 callbacks:{},addEventListener(name,fn){this.callbacks[name]=fn;},querySelectorAll(){return [];},setAttribute(){},scrollIntoView(){}});
const q=id=>{if(!elements.has(id))elements.set(id,make());return elements.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const candidate={svg:'<svg/>',coverage:{uncovered_cell_count:0},installation_notes:[]};
const point={additional_mass_kg:110,physical_bar_count:35,position_count:3,zone_count:7,bar_schedule:[],
 direction_candidate_indexes:[0,0,0,0],stock_cutting:{status:'pass',groups:[]}};
context.payload={front:[point],directions:Array.from({length:4},()=>({candidates:[candidate]})),blocking_check_ids:['xy-layer-order'],
 engineering_example:{reference:{mass_kg:100,physical_bar_count:30,position_count:4,scope:'Straight and shaped'}}};
vm.runInContext('result=payload;renderPoint()',context);
assert(q('#metrics').innerHTML.includes('Физические стержни'));assert(q('#metrics').innerHTML.includes('Позиции спецификации'));
assert(q('#metrics').innerHTML.includes('7 параметрических зон'));assert(q('#comparison-rows').innerHTML.includes('+10 кг'));
assert(q('#comparison-scope').textContent.includes('Straight and shaped'));assert.equal(q('#engineer-comparison').hidden,false);
assert(q('#check-summary').innerHTML.includes('Нужна независимая проверка'));assert(q('#blockers').innerHTML.includes('Порядок и высоты'));
assert.equal(q('#drawing').dataset.view,'source');assert(q('#drawing-view-note').textContent.includes('параметрические зоны до физической обработки'));
q('#drawing-views').callbacks.click({target:{closest:()=>({dataset:{view:'physical'}})}});
assert.equal(q('#drawing').dataset.view,'physical');assert(q('#drawing-view-note').textContent.includes('не обрезаются'));
q('#drawing-layers').callbacks.click({target:{closest:()=>({dataset:{layer:'demand'}})}});
assert.equal(q('#drawing').dataset.layer,'demand');assert(q('#drawing-legend').textContent.includes('скрыта только на схеме'));
vm.runInContext('setZoom(9)',context);assert.equal(q('#zoom-level').textContent,'300%');
vm.runInContext('setZoom(-9)',context);assert.equal(q('#zoom-level').textContent,'100%');
context.payload.front=[];vm.runInContext('result=payload;renderPoint()',context);
assert.equal(q('#metrics').innerHTML,'');assert.equal(q('#download-selected').disabled,true);assert.equal(q('#engineer-comparison').hidden,true);
"""
    node(script, STATIC / "composite.js")


def test_source_physical_toggle_keeps_original_parameters_separate_and_preserves_checks():
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const elements=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 callbacks:{},addEventListener(name,fn){this.callbacks[name]=fn;},querySelectorAll(){return [];},setAttribute(){},scrollIntoView(){}});
const q=id=>{if(!elements.has(id))elements.set(id,make());return elements.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const zone={source_zone_id:'<original-zone>',direction:{axis:'Y'},demand_bbox_mm:[0,0,800,3900],components:[{
 component_index:0,diameter_mm:25,nominal_step_mm:150,installed_length_mm:5850,axis_window_mm:[0,800],
 bar_count:6,axis_coordinates_mm:[0,100,300,400,600,700]}]};
const candidate={svg:'<svg>physical-complete-outside-host</svg>',overlay_svg:'<svg>source-zones-plus-actual-bars</svg>',zone_drafts:[],physical_bars:[{},{}],coverage:{uncovered_cell_count:0},installation_notes:[]};
const point={additional_mass_kg:110,physical_bar_count:8,position_count:3,zone_count:4,bar_schedule:[],
 direction_candidate_indexes:[0,0,0,0],stock_cutting:{status:'fail',groups:[]}};
context.payload={output_kind:'normalized-physical-bars',front:[point],directions:Array.from({length:4},()=>({
 candidates:[candidate],source_svg:'<svg>original-polygons-and-rectangles</svg>',source_zone_drafts:[zone]})),
 source_graphics:{directions:Array.from({length:4},()=>({legend:[{level_index:1,label:'<level>',rgb:[255,0,0]}]}))},
 blocking_check_ids:['xy-layer-order'],same_plane_conflicts:{body_intersection_count:12}};
vm.runInContext('result=payload;renderPoint()',context);
assert.equal(q('#drawing').innerHTML,'<svg>original-polygons-and-rectangles</svg>');
assert.equal(q('#source-zone-details').hidden,false);
assert(q('#drawing-view-note').textContent.includes('параметрические зоны до физической обработки'));
q('#drawing-views').callbacks.click({target:{closest:()=>({dataset:{view:'source'}})}});
assert.equal(q('#drawing').innerHTML,'<svg>original-polygons-and-rectangles</svg>');
assert(q('#source-zone-rows').innerHTML.includes('3 900 × 800'));
assert(q('#source-zone-rows').innerHTML.includes('5 850 × 800'));
assert(q('#source-zone-rows').innerHTML.includes('100 / 200'));assert(q('#source-zone-rows').innerHTML.includes('&lt;original-zone&gt;'));
assert(q('#source-zone-summary').textContent.includes('6 стержней до'));assert(!q('#source-legend').innerHTML.includes('<level>'));
assert(q('#source-legend').innerHTML.includes('rgb(255,0,0)'));assert.equal(q('#download-source').disabled,false);
assert.equal(q('#download-selected').disabled,true);
vm.runInContext('download=(value,filename)=>{window.sourceDownloaded={value,filename};}',context);
q('#download-source').callbacks.click();
assert.equal(context.window.sourceDownloaded.value,context.payload.source_graphics);
assert.equal(context.window.sourceDownloaded.filename,'source-isofields-zones.json');
q('#source-envelopes').checked=true;q('#source-envelopes').callbacks.change();
assert.equal(q('#drawing').dataset.envelopes,'shown');assert(q('#drawing-legend').textContent.includes('не тела стали и не AreaBoundary'));
assert(q('#metrics-scope').textContent.includes('физическая партия'));assert(q('#check-summary').innerHTML.includes('12 пар'));
assert.equal(q('#download-selected').disabled,true);assert(q('#handoff-note').textContent.includes('не сформирован'));
const oldMetrics=q('#metrics').innerHTML, oldChecks=q('#check-summary').innerHTML;
q('#drawing-views').callbacks.click({target:{closest:()=>({dataset:{view:'combined'}})}});
assert.equal(q('#drawing').innerHTML,'<svg>source-zones-plus-actual-bars</svg>');
assert.equal(q('#metrics').innerHTML,oldMetrics);assert.equal(q('#check-summary').innerHTML,oldChecks);
q('#drawing-views').callbacks.click({target:{closest:()=>({dataset:{view:'physical'}})}});
assert.equal(q('#drawing').innerHTML,'<svg>physical-complete-outside-host</svg>');assert.equal(q('#source-zone-details').hidden,true);
assert(q('#drawing-view-note').textContent.includes('не обрезаются'));assert.equal(q('#metrics').innerHTML,oldMetrics);assert.equal(q('#check-summary').innerHTML,oldChecks);
q('#drawing-views').callbacks.click({target:{closest:()=>({dataset:{view:'source'}})}});
assert.equal(q('#drawing').innerHTML,'<svg>original-polygons-and-rectangles</svg>');assert.equal(q('#source-zone-details').hidden,false);
// A source-only report can retain several alternatives: never reuse the selected one's cached geometry for another.
context.payload.source_graphics_candidate_index=0;context.payload.output_kind='source-zones-only';
context.payload.front.push({...point,direction_candidate_indexes:[1,1,1,1]});
context.payload.directions.forEach(d=>d.candidates.push({...candidate,svg:'<svg>other-original-candidate</svg>',zone_drafts:[{...zone,source_zone_id:'other-zone'}]}));
q('#candidate').value='1';vm.runInContext('renderPoint()',context);
assert.equal(q('#drawing').innerHTML,'<svg>other-original-candidate</svg>');assert(q('#source-zone-rows').innerHTML.includes('other-zone'));
assert.equal(q('#download-source').disabled,true);
assert(q('#handoff-note').textContent.includes('пакет зон не подготовлен'));
"""
    node(script, STATIC / "composite.js")


def test_trim_result_never_promotes_geometry_presence_to_anchor_or_reuses_old_packet():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();const make=()=>({innerHTML:'',textContent:'',value:'0',disabled:false,hidden:false,dataset:{},style:{},
 callbacks:{},addEventListener(name,fn){this.callbacks[name]=fn;},querySelectorAll(){return [];},setAttribute(){},scrollIntoView(){}});
const q=id=>{if(!elements.has(id))elements.set(id,make());return elements.get(id);};
const context={document:{querySelector:q,body:{classList:{add(){},remove(){}}}},window:{location:{search:'',hash:''},engineeringExampleReady:Promise.resolve(null)},
 URLSearchParams,Intl,console,setTimeout,clearTimeout,setInterval,clearInterval};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const candidate={svg:'<svg>ACTUAL-CUTS-WITH-UNRESOLVED-OUTSIDE</svg>',coverage:{uncovered_cell_count:2},geometric_presence:{uncovered_cell_count:0},zone_drafts:[],installation_notes:[]};
const point={additional_mass_kg:90,physical_bar_count:10,position_count:4,zone_count:3,bar_schedule:[],
 direction_candidate_indexes:[0,0,0,0],stock_cutting:{status:'not_checked',reason:'cutting_position_budget_exceeded'}};
context.payload={output_kind:'boundary-trimmed-physical-bars',front:[point],directions:Array.from({length:4},()=>({candidates:[candidate],
 source_svg:'<svg>ORIGINAL</svg>',source_zone_drafts:[]})),source_graphics:{directions:Array.from({length:4},()=>({legend:[]}))},
 blocking_check_ids:['boundary-trim-engineering-review','original_FE_with_control_40d'],
 boundary_trim:{geometric_presence:{status:'pass',uncovered_cell_count:0},coverage_with_control_40d:{status:'fail',uncovered_cell_count:8},
 external_boundary_failures_before:10,external_boundary_failures_after:1,collisions:{proven_collision_pair_count:0,uncertain_pair_count:0},
 stock_cutting:{status:'not_checked'},actual_Revit_host_informational_failures:7},
 graphic_bar_plan_draft:{schema_version:'graphic-bar-plan-draft/v1',placement_eligible:false},physical_trial_packet:{stale:true}};
vm.runInContext('result=payload;renderPoint()',context);
assert(q('#check-summary').innerHTML.includes('Наличие стали на исходных КЭ — не анкеровка'));
assert(q('#check-summary').innerHTML.includes('8 КЭ'));assert(q('#check-summary').innerHTML.includes('После')===false);
assert(q('#check-summary').innerHTML.includes('после: 1'));assert(q('#metrics-scope').textContent.includes('физическая партия'));
assert(q('#direction-status').textContent.includes('с прежними 40d — 2'));
assert.equal(q('#drawing').dataset.view,'source');
assert.equal(q('#download-selected').disabled,false);assert(q('#handoff-note').textContent.includes('SourceWorkflow'));
assert.equal(q('#drawing').innerHTML,'<svg>ORIGINAL</svg>');
q('#drawing-views').callbacks.click({target:{closest:()=>({dataset:{view:'physical'}})}});
assert(q('#drawing-view-note').textContent.includes('не обрезка картинки'));
assert.equal(q('#drawing').innerHTML,'<svg>ACTUAL-CUTS-WITH-UNRESOLVED-OUTSIDE</svg>');
vm.runInContext('download=(value,filename)=>{window.downloaded={value,filename};}',context);
q('#download-selected').callbacks.click();
assert.equal(context.window.downloaded.value.schema_version,'graphic-bar-plan-draft/v1');
assert.equal(context.window.downloaded.value.physical_trial_packet,undefined);
assert.equal(context.window.downloaded.filename,'graphic-bar-plan-draft.json');
context.payload.graphic_bar_plan_draft=null;context.window.downloaded=null;
vm.runInContext('renderPoint()',context);assert.equal(q('#download-selected').disabled,true);
q('#download-selected').callbacks.click();assert.equal(context.window.downloaded,null);
// A pruned batch has a NEW wrapper, not an incomplete exact-trim inventory.
context.payload.output_kind='pruned-trimmed-physical-bars';
context.payload.trimmed_cleanup={accepted_nonregression:true,removed_bar_count:2,
 physical_metrics_before:{physical_bar_count:12},physical_metrics:{physical_bar_count:10},
 coverage_after:{geometric_presence:{status:'fail',uncovered_cell_count:3},control_40d:{status:'fail',uncovered_cell_count:8}},
 stock_cutting:{status:'fail'},collisions:{proven_collision_pair_count:2,uncertain_pair_count:0},material_boundary_failures_after:0};
context.payload.graphic_bar_plan_pruned={schema_version:'graphic-bar-plan-pruned/v1',placement_eligible:false,
 source_trim_packet:{history:true},retained_bar_ids:[{direction:'top-X',bar_id:'retained'}]};
const historical=JSON.stringify(context.payload.boundary_trim);
vm.runInContext('renderPoint()',context);
assert.equal(q('#download-selected').disabled,false);
assert(q('#check-summary').innerHTML.includes('удалено 2'));
assert(q('#check-summary').innerHTML.includes('3 КЭ'));
assert(q('#check-summary').innerHTML.includes('2 пересечений'));
assert(q('#check-summary').innerHTML.includes('Старый счётчик защитного слоя'));
assert.equal(JSON.stringify(context.payload.boundary_trim),historical);
q('#download-selected').callbacks.click();
assert.equal(context.window.downloaded.filename,'graphic-bar-plan-pruned.json');
assert.equal(context.window.downloaded.value.source_trim_packet.history,true);
context.window.downloaded=null;
context.payload.graphic_bar_plan_pruned.schema_version='graphic-bar-plan-draft/v1';
vm.runInContext('renderPoint()',context);assert.equal(q('#download-selected').disabled,true);
q('#download-selected').callbacks.click();assert.equal(context.window.downloaded,null);
"""
    node(script,STATIC / "composite.js")
