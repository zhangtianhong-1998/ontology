"""Exercise actual viewer JS with the published 258-node topology and DOM stubs."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "code/ontology_r2/viewer.html"


def js(body, data=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for viewer geometry checks")
    completed = subprocess.run([node, "-e", "const assert=require('node:assert/strict');\n" + body],
                               input=json.dumps(data or {}), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def helpers(start, end):
    page = PAGE.read_text()
    return page[page.index(start):page.index(end)]


GEOMETRY_SETUP = """
const NODE_RADIUS=40,treeLayoutCache=new Map(),name=x=>x.label||x.id;
function boundsFor(nodes,padding=75){const xs=nodes.map(n=>n.x),ys=nodes.map(n=>n.y);return{x:Math.min(...xs)-padding,y:Math.min(...ys)-padding,width:Math.max(...xs)-Math.min(...xs)+2*padding,height:Math.max(...ys)-Math.min(...ys)+2*padding}}
"""


def test_published_258_nodes_pack_without_collisions_and_routes_avoid_nodes():
    fixture = json.loads((ROOT / "tests/fixtures/viewer_v8_topology.json").read_text())
    js(GEOMETRY_SETUP + helpers("function packBoxes(", "function drawGraph(") + """
const D=JSON.parse(require('fs').readFileSync(0,'utf8'));
assert.equal(D.nodes.length,258);assert.equal(D.edges.length,253);
for(const [width,height] of [[1200,700],[800,600],[600,640]]){
  const positions=ontologyPositions(D.nodes,width/height),nodes=D.nodes.map(n=>({id:n.id,...positions.get(n.id)})),bounds=boundsFor(nodes,120);
  assert.equal(positions.size,258);assert(bounds.height/bounds.width<3,'old layout was an unreadable 15:1 strip');
  for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++)assert(Math.hypot(nodes[i].x-nodes[j].x,nodes[i].y-nodes[j].y)>=80);
  const routes=routeGraphEdges(nodes,D.edges);assert.equal(routes.length,D.edges.length);
  routes.forEach((path,i)=>{assert(path.length>=2);for(let j=1;j<path.length;j++)assert(segmentClear(path[j-1],path[j],nodes,new Set([D.edges[i].from,D.edges[i].to]),40),'edge crosses an unrelated node')});
}
""", fixture)


def test_layout_preserves_real_parent_topology_and_separates_parallel_edges():
    js(GEOMETRY_SETUP + helpers("function packBoxes(", "function drawGraph(") + """
const types=[{id:'r'},{id:'a',parent:'r'},{id:'b',parent:'r'},{id:'c',parent:'a'},{id:'d',parent:'a'}],p=ontologyPositions(types);
assert(p.get('a').y>p.get('r').y);assert(p.get('c').y>p.get('a').y);assert(p.get('d').y>p.get('a').y);
const nodes=[{id:'a',x:0,y:0},{id:'b',x:300,y:0}],edges=[{from:'a',to:'b'},{from:'a',to:'b'}],paths=routeGraphEdges(nodes,edges);
assert.notDeepEqual(paths[0],paths[1]);
// Damaged parent cycles do not hang the viewer or erase nodes.
assert.equal(ontologyPositions([{id:'x',parent:'y'},{id:'y',parent:'x'}]).size,2);
""")


CAMERA_SETUP = """
let size={width:1200,height:700},selected=null,mode='ontology';const A=new Map(),pinnedPositions={ontology:new Map(),metadata:new Map()},treeLayoutCache=new Map();
let renders=0,observer=null;const graphState={nodes:[],edges:[],view:null,fit:null,bounds:null,userViewChanged:false,drag:null,suppressClickUntil:0};
const events={},classes=new Set(),elements={graph:{style:{setProperty(){}},setAttribute(k,v){this[k]=v},getBoundingClientRect(){return{left:0,top:0,...size}},addEventListener(k,v){events[k]=v},setPointerCapture(){}}};
const $=id=>elements[id]||(elements[id]={setAttribute(){},classList:{add(...a){a.forEach(x=>classes.add(x))},remove(...a){a.forEach(x=>classes.delete(x))}}}),viewportSize=()=>size;
const renderGraph=()=>{renders++;for(const n of graphState.nodes){const p=pinnedPositions[mode].get(n.id);if(p)Object.assign(n,p)}},ResizeObserver=class{constructor(callback){observer=callback}observe(){}};
"""


def camera_helpers():
    return (helpers("function fittedView(", "function zoomAtCenter(")
            + helpers("function pointInGraph(", "// Pack actual subtrees"))


def test_camera_can_read_tall_graph_and_search_focuses_offscreen_node():
    js(CAMERA_SETUP + camera_helpers() + """
graphState.bounds={x:0,y:0,width:694,height:10825};fitGraph();zoomGraph(1e6,600,350);
assert(size.width/graphState.view.width>=2.9,'maximum zoom must reach physical readability');
graphState.nodes=[{id:'target',x:4000,y:9000},{id:'neighbor',x:4200,y:9000}];graphState.edges=[{from:'target',to:'neighbor'}];selected={kind:'object',id:'target'};focusSelection();
let v=graphState.view;assert(v.x<4000&&v.x+v.width>4000&&v.y<9000&&v.y+v.height>9000);assert(size.width/v.width>=.95);
const center=[v.x+v.width/2,v.y+v.height/2];size={width:600,height:640};observer();v=graphState.view;assert(Math.abs(v.x+v.width/2-center[0])<1e-7&&Math.abs(v.y+v.height/2-center[1])<1e-7);
graphState.userViewChanged=false;const before=renders;observer();assert(renders>before);assert.equal(graphState.userViewChanged,false);
""")


def test_drag_pin_release_relayout_and_blank_canvas_pan_execute_handlers():
    js(CAMERA_SETUP + camera_helpers() + """
graphState.bounds={x:0,y:0,width:1200,height:700};fitGraph();graphState.nodes=[{id:'a',x:100,y:100}];selected={kind:'object',id:'a'};
const nodeElement={getAttribute(){return'a'}},target={closest(selector){return selector==='.node'?nodeElement:null}};
events.pointerdown({button:0,pointerId:1,clientX:100,clientY:100,target});events.pointermove({pointerId:1,clientX:200,clientY:150});events.pointerup({pointerId:1});
assert.deepEqual(pinnedPositions.ontology.get('a'),{x:200,y:150});assert.equal(graphState.drag,null);assert(graphState.suppressClickUntil>Date.now());assert(!classes.has('is-dragging'));
$('pin-selection').onclick();assert(!pinnedPositions.ontology.has('a'));$('pin-selection').onclick();assert(pinnedPositions.ontology.has('a'));$('relayout').onclick();assert.equal(pinnedPositions.ontology.size,0);assert.equal(graphState.userViewChanged,false);
const old={...graphState.view};events.pointerdown({button:0,pointerId:2,clientX:0,clientY:0,target:{closest(){return null}}});events.pointermove({pointerId:2,clientX:100,clientY:50});events.pointercancel({pointerId:2});assert(graphState.view.x<old.x&&graphState.view.y<old.y);assert.equal(graphState.drag,null);
""")


def test_selection_calls_focus_after_rendering_and_view_is_offline():
    page = PAGE.read_text()
    select = page[page.index("function select("):page.index("// Inherited properties")]
    assert "renderGraph();renderDetails();focusSelection()" in select
    assert "https://" not in page and "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    assert "full-node-label" in page and "pinnedPositions[mode]" in page
