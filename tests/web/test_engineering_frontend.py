"""Frontend contracts and JS behavior with synthetic DOM doubles, not Revit checks."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src/rebar/web/static"


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
    assert "Рассчитать плиту К09" in html
    assert html.index('id="run-engineering-example"') < html.index('id="analysis-form"')
    assert 'href="/composite#custom-inputs"' in html


def test_composite_keeps_all_controls_but_checks_are_visible_and_metrics_unambiguous():
    page = Page("composite.html")
    for key in ("composite-form", "run", "run-demo", "run-engineering-example", "example-files",
            "metrics", "drawing", "candidate", "stock-status", "balance-status", "stock-patterns",
            "direction-tabs", "installation-notes", "schedule", "blockers", "download-report", "download-selected",
            "check-summary", "engineer-comparison", "comparison-rows", "drawing-layers", "zoom-reset"):
        assert key in page.ids
    assert page.details == {"check-summary": [], "blockers": []}
    assert "open" not in page.ids["custom-inputs"][1]
    html = (STATIC / "composite.html").read_text(encoding="utf-8")
    assert "Синтетический пример" in html and "Черновик · не размещён в Revit" in html
    assert html.index('id="output"') < html.index('id="run-demo"')
    assert not page.stack


def node(script, *args):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node unavailable; browser smoke is separate")
    return subprocess.run([executable, "-e", script, *map(str, args)],
        text=True, capture_output=True, check=True, timeout=10)


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
class Element {constructor(){this.textContent='';this.disabled=true;this.children=[];}append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}}
const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const example={id:'k09-typical-3-14',is_available:true,source_kind:'real_engineering_files',title:'Real plate',
profile:{engineering_approval:false,note:'Chosen research phases, not approved'},
reference:{mass_kg:100,physical_bar_count:30,position_count:4,scope:'Full engineer scope',note:'Not an approval'},
sources:[{direction:{layer:'bottom',axis:'X'},dxf_filename:'Настоящая нижняя.dxf',shk_filename:null,mapping_label:'Проверенная шкала'}]};
const state=process.argv[2];if(state==='unavailable'){example.is_available=false;example.status='Actual source checksum mismatch';}if(state==='synthetic')example.source_kind='synthetic';if(state==='bad-id')example.id='../secret';
const context={window:{location:{search:''}},document:{getElementById:get,createElement:()=>new Element()},URLSearchParams,Intl,
fetch:async url=>{assert.equal(url,'/api/engineering-examples');return {ok:state!=='api-error',json:async()=>({examples:state==='missing'?[]:[example]})};}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.window.engineeringExampleReady.then(value=>{assert.equal(get('run-engineering-example').disabled,state!=='available');
 if(state==='available'){assert.equal(value.id,example.id);assert(get('example-reference-note').textContent.includes('Full engineer scope'));assert(get('example-profile-note').textContent.includes('Chosen research phases'));
 const row=get('example-files').children[0];assert.equal(row.children[1].textContent,'Настоящая нижняя.dxf');assert.equal(row.children[2].textContent,'Проверенная шкала');}
 else {assert.equal(value,null);if(state==='unavailable')assert(get('example-status').textContent.includes('Actual source checksum mismatch'));}
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
    node(script, STATIC / "engineering-example.js", state)


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
q('#drawing-layers').callbacks.click({target:{closest:()=>({dataset:{layer:'demand'}})}});
assert.equal(q('#drawing').dataset.layer,'demand');assert(q('#drawing-legend').textContent.includes('скрыта только на схеме'));
vm.runInContext('setZoom(9)',context);assert.equal(q('#zoom-level').textContent,'300%');
vm.runInContext('setZoom(-9)',context);assert.equal(q('#zoom-level').textContent,'100%');
context.payload.front=[];vm.runInContext('result=payload;renderPoint()',context);
assert.equal(q('#metrics').innerHTML,'');assert.equal(q('#download-selected').disabled,true);assert.equal(q('#engineer-comparison').hidden,true);
"""
    node(script, STATIC / "composite.js")
