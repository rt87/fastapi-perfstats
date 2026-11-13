"""Live (auto-refreshing) stats page: refresh selector, table merge script and per-route sparklines (#16).

The selector is always rendered; ``StatsConfig.refresh_seconds`` is its initial value (``None`` and ``0`` = Off).
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from fastapi_perfstats.config import StatsConfig
from fastapi_perfstats.ext.base import Column, NoopExtension, PagePart

if TYPE_CHECKING:
    from fastapi_perfstats.collector import StatsCollector

_CHOICES = ((0, "Off"), (5, "5 s"), (15, "15 s"), (60, "60 s"))
_WIDTH = 120
_HEIGHT = 20
_PAD = 2

_JS = """
(function(){
  var sel=document.getElementById('refresh'),note=document.getElementById('refresh-note');
  if(!sel)return;
  var KEY='fastapi-perfstats-refresh',timer=null,busy=false,state={};
  function valid(v){return v==='0'||v==='5'||v==='15'||v==='60';}
  try{var saved=localStorage.getItem(KEY);if(saved!==null&&valid(saved))sel.value=saved;}catch(e){}
  document.querySelectorAll('table.s').forEach(function(t){
    var st=state[t.id]={i:-1,dir:1};
    t.tHead.querySelectorAll('th').forEach(function(th,i){
      th.addEventListener('click',function(){st.dir=-st.dir;st.i=i;});
    });
  });
  function resort(t){
    var st=state[t.id],tb=t.tBodies[0];
    if(!st||st.i<0)return;
    Array.from(tb.rows).sort(function(a,b){
      var x=a.cells[st.i],y=b.cells[st.i],p=x.dataset.v,q=y.dataset.v;
      if(p!==undefined&&q!==undefined)return (parseFloat(p)-parseFloat(q))*st.dir;
      return x.textContent.localeCompare(y.textContent)*st.dir;
    }).forEach(function(r){tb.appendChild(r);});
  }
  function refresh(){
    if(busy)return;
    busy=true;
    fetch(location.href,{credentials:'same-origin',cache:'no-store'}).then(function(r){
      if(!r.ok)throw new Error('http '+r.status);
      return r.text();
    }).then(function(text){
      var doc=new DOMParser().parseFromString(text,'text/html');
      document.querySelectorAll('table.s').forEach(function(t){
        var fresh=doc.getElementById(t.id);
        if(!fresh)return;
        var tb=t.tBodies[0];
        tb.replaceChildren.apply(tb,Array.from(fresh.tBodies[0].childNodes).map(function(n){
          return document.importNode(n,true);
        }));
        resort(t);
      });
      document.querySelectorAll('input.f').forEach(function(inp){inp.dispatchEvent(new Event('input'));});
      note.textContent='';
    }).catch(function(){
      note.textContent='refresh failed';
    }).then(function(){busy=false;});
  }
  function apply(){
    if(timer!==null){clearInterval(timer);timer=null;}
    var s=parseInt(sel.value,10);
    if(s>0)timer=setInterval(refresh,s*1000);
  }
  sel.addEventListener('change',function(){
    try{localStorage.setItem(KEY,sel.value);}catch(e){}
    apply();
  });
  apply();
})();
"""


def _html_top(collector: StatsCollector) -> str:
    current = collector.config.refresh_seconds or 0
    options = "".join(
        f'<option value="{seconds}"{" selected" if seconds == current else ""}>{escape(label)}</option>'
        for seconds, label in _CHOICES
    )
    return (
        '<p><label>Auto-refresh <select id="refresh">' + options + "</select></label> "
        '<span id="refresh-note" class="mut"></span></p>'
    )


def _body_attrs(config: StatsConfig) -> dict[str, str]:
    return {"data-refresh": str(config.refresh_seconds or 0)}


def sparkline(series: list[int], minutes: int) -> str:
    """Inline SVG of ``series`` (calls per minute, oldest first); an empty or all-zero series is a flat line."""
    peak = max(series, default=0)
    n = len(series)
    usable = _HEIGHT - 2 * _PAD
    points = []
    for i, v in enumerate(series):
        x = _WIDTH * i / (n - 1) if n > 1 else _WIDTH / 2
        y = _HEIGHT - _PAD - (usable * v / peak if peak else 0)
        points.append(f"{x:.1f},{y:.1f}")
    if not points:
        points = [f"0,{_HEIGHT - _PAD}", f"{_WIDTH},{_HEIGHT - _PAD}"]
    label = escape(f"calls per minute, last {minutes} min, max {peak}")
    return (
        f'<svg width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="{label}">'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="currentColor" stroke-width="1"/></svg>'
    )


class _LiveExtension(NoopExtension):
    def columns(self, config: StatsConfig) -> tuple[Column, ...]:
        if config.recent_minutes <= 0:
            return ()
        minutes = config.recent_minutes
        return (
            Column(
                "freq",
                f"Last {minutes} min",
                25,
                lambda r, c: (sparkline(r.series, minutes), float(sum(r.series))),
                raw_html=True,
            ),
        )


PART = PagePart(name="live", html_top=_html_top, js=_JS, body_attrs=_body_attrs)
EXT = _LiveExtension("live")
