const state = { events: [], filters: { categories: [], sources: [] } };

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, ch => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[ch]));

function toast(message){
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(window.__toast);
  window.__toast = setTimeout(() => node.classList.remove("show"), 2200);
}

function riskClass(value){
  const text = String(value || "");
  if (text.includes("高")) return "high";
  if (text.includes("中")) return "mid";
  if (text.includes("低")) return "low";
  return "";
}

function dateText(value){
  const text = String(value || "").trim();
  if (!text) return "暂无";
  return text.slice(0, 10);
}

async function getJSON(url){
  const response = await fetch(url, { headers: { "Accept": "application/json" } });
  if (!response.ok) throw new Error("request failed");
  return response.json();
}

function switchView(id){
  document.querySelectorAll(".view").forEach(node => node.classList.toggle("active", node.id === id));
  document.querySelectorAll(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.view === id));
  const title = {dashboard:"平台概览",events:"事件浏览",sources:"数据来源",about:"关于项目"}[id] || "PortScope";
  $("pageTitle").textContent = title;
  window.scrollTo({top:0, behavior:"smooth"});
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelectorAll("[data-jump]").forEach(button => button.addEventListener("click", () => switchView(button.dataset.jump)));

function drawTrend(items){
  const canvas = $("trendChart");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const p = {l:48,r:24,t:24,b:42};
  ctx.clearRect(0,0,W,H);

  ctx.strokeStyle = "#e6ebf2";
  ctx.lineWidth = 1;
  for(let i=0;i<5;i++){
    const y = p.t + i*(H-p.t-p.b)/4;
    ctx.beginPath(); ctx.moveTo(p.l,y); ctx.lineTo(W-p.r,y); ctx.stroke();
  }

  if(!items.length){
    ctx.fillStyle="#8190a4"; ctx.font="12px system-ui"; ctx.fillText("暂无可绘制的事件日期数据",p.l,70); return;
  }

  const max = Math.max(1,...items.map(x=>Number(x.events)||0));
  ctx.beginPath();
  items.forEach((item,index)=>{
    const x = items.length === 1 ? (W/2) : p.l + index*(W-p.l-p.r)/(items.length-1);
    const y = H-p.b-(Number(item.events)||0)/max*(H-p.t-p.b);
    index ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
  });
  ctx.strokeStyle="#2e6be6"; ctx.lineWidth=3; ctx.stroke();

  items.forEach((item,index)=>{
    const x = items.length === 1 ? (W/2) : p.l + index*(W-p.l-p.r)/(items.length-1);
    const y = H-p.b-(Number(item.events)||0)/max*(H-p.t-p.b);
    ctx.fillStyle="#fff";ctx.strokeStyle="#2e6be6";ctx.lineWidth=2;
    ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.fillStyle="#7d8a9d";ctx.font="10px system-ui";
    ctx.fillText(String(item.date).slice(5),x-13,H-17);
  });

  ctx.fillStyle="#8794a7";ctx.font="10px system-ui";
  ctx.fillText(String(max),12,p.t+4);ctx.fillText("0",22,H-p.b+4);
}

function eventRow(event){
  const title = esc(event.title || "公开信息");
  const source = esc(event.source_name || "公开来源");
  const summary = esc(event.summary || "暂无摘要");
  const risk = esc(event.risk_level || "未标记");
  return `<div class="event-row">
    <div><h3>${title}</h3><p>${summary}</p></div>
    <div class="meta">${source}</div>
    <div><span class="risk-tag ${riskClass(risk)}">${risk}</span></div>
    <button class="detail-button" data-event-key="${esc(event.event_key)}">→</button>
  </div>`;
}

function eventCard(event){
  return `<article class="event-card">
    <div class="event-card-top">
      <span class="status-tag">${esc(event.category || "未分类")}</span>
      <span class="risk-tag ${riskClass(event.risk_level)}">${esc(event.risk_level || "未标记")}</span>
    </div>
    <h3>${esc(event.title || "公开信息")}</h3>
    <p>${esc(event.summary || "暂无摘要")}</p>
    <div class="event-card-meta">
      <span>${esc(dateText(event.event_date))}</span>
      <span>${esc(event.source_name || "公开来源")}</span>
      <span>${event.evidence_verified ? "证据已定位" : "证据待核对"}</span>
    </div>
    <div class="event-card-actions">
      <span class="status-tag">${event.human_verified ? "已独立人工复核" : "AI辅助抽取"}</span>
      <button class="text-button" data-event-key="${esc(event.event_key)}">查看证据 →</button>
    </div>
  </article>`;
}

async function loadDashboard(){
  try{
    const data = await getJSON("/api/dashboard");
    $("documents").textContent = data.documents;
    $("qualified").textContent = data.qualified_documents;
    $("eventCount").textContent = data.events;
    $("sourceCount").textContent = data.enabled_sources;
    $("pendingReview").textContent = data.pending_review + " 条";
    $("latestUpdate").textContent = dateText(data.latest_update);
    const rate = data.documents ? (data.qualified_documents/data.documents*100).toFixed(1) : "0.0";
    $("qualityRate").textContent = `质量通过率 ${rate}%`;

    $("riskTags").innerHTML = Object.entries(data.risk || {}).map(([key,value]) =>
      `<span class="risk-tag ${riskClass(key)}">${esc(key)} ${value}</span>`
    ).join("") || '<span class="status-tag">暂无标签</span>';
    drawTrend(data.trend || []);

    const eventData = await getJSON("/api/events?limit=5");
    $("latestEvents").innerHTML = eventData.items.length ? eventData.items.map(eventRow).join("") : '<div class="empty">暂无可展示事件</div>';
    bindEventButtons();
  }catch(error){
    $("latestEvents").innerHTML = '<div class="empty">演示数据暂时无法加载</div>';
    drawTrend([]);
  }
}

function fillSelect(select, values, placeholder){
  select.innerHTML = `<option value="">${placeholder}</option>` + values.map(value =>
    `<option value="${esc(value)}">${esc(value)}</option>`
  ).join("");
}

async function loadEvents(){
  const params = new URLSearchParams();
  const keyword = $("keywordInput").value.trim();
  const category = $("categorySelect").value;
  const source = $("sourceSelect").value;
  if(keyword) params.set("keyword",keyword);
  if(category) params.set("category",category);
  if(source) params.set("source",source);
  params.set("limit","100");

  $("eventLibrary").innerHTML = '<div class="empty">正在检索…</div>';
  try{
    const data = await getJSON("/api/events?" + params.toString());
    state.events = data.items || [];
    state.filters = data.filters || {categories:[],sources:[]};
    if($("categorySelect").options.length <= 1) fillSelect($("categorySelect"),state.filters.categories || [],"全部事件类型");
    if($("sourceSelect").options.length <= 1) fillSelect($("sourceSelect"),state.filters.sources || [],"全部来源");
    $("resultCount").textContent = `找到 ${data.count} 条演示事件`;
    $("eventLibrary").innerHTML = state.events.length ? state.events.map(eventCard).join("") : '<div class="empty">没有匹配结果，请调整筛选条件。</div>';
    bindEventButtons();
  }catch(error){
    $("eventLibrary").innerHTML = '<div class="empty">数据暂时无法加载</div>';
  }
}

async function openEvent(key){
  const event = state.events.find(item => item.event_key === key) || (await getJSON("/api/events?limit=200")).items.find(item => item.event_key === key);
  if(!event){ toast("未找到该事件"); return; }

  let evidence = [];
  try{ evidence = (await getJSON(`/api/events/${encodeURIComponent(key)}/evidence`)).items || []; }catch(error){}

  const evidenceHTML = evidence.length ? evidence.map((item,index) =>
    `<div class="evidence-item"><p><b>证据 ${index+1}</b>　${esc(item.quote_text || "")}</p><small>${esc(item.verification_status || "待核对")}</small></div>`
  ).join("") : '<div class="empty">当前演示记录没有可展示的逐字短证据。</div>';

  const sourceLink = /^https?:\/\//.test(String(event.source_url || "")) ?
    `<a class="source-link" target="_blank" rel="noopener noreferrer" href="${esc(event.source_url)}">打开原始来源 ↗</a>` : "";

  $("dialogBody").innerHTML = `<div class="dialog-content">
    <span class="eyebrow">EVENT DETAIL / EVIDENCE</span>
    <h2>${esc(event.title || "公开信息")}</h2>
    <div class="dialog-meta">
      <span>${esc(dateText(event.event_date))}</span><span>${esc(event.category || "未分类")}</span>
      <span>${esc(event.source_name || "公开来源")}</span><span>${esc(event.risk_level || "未标记")}</span>
    </div>
    <p class="summary">${esc(event.summary || "暂无摘要")}</p>
    ${event.impact ? `<p class="summary"><b>分析提示：</b>${esc(event.impact)}</p>` : ""}
    ${sourceLink}
    <div class="evidence-block"><span class="eyebrow">TRACEABLE EVIDENCE</span>${evidenceHTML}</div>
  </div>`;
  $("eventDialog").showModal();
}

function bindEventButtons(){
  document.querySelectorAll("[data-event-key]").forEach(button => {
    button.onclick = () => openEvent(button.dataset.eventKey);
  });
}

async function loadSources(){
  try{
    const data = await getJSON("/api/sources");
    $("sourceGrid").innerHTML = data.items.length ? data.items.map(item => {
      const link = /^https?:\/\//.test(String(item.homepage_url || "")) ?
        `<a target="_blank" rel="noopener noreferrer" href="${esc(item.homepage_url)}">访问官方网站 ↗</a>` : "";
      return `<article class="source-card">
        <span class="status-tag">${item.enabled ? "已启用" : "演示库未启用"}</span>
        <h3>${esc(item.source_name || "公开来源")}</h3>
        <p>${esc(item.organization || "")} · ${esc(item.region || "")}</p>
        <p>主要信息类型：${esc(item.category_hint || item.source_type || "公开信息")}</p>
        ${link}
      </article>`;
    }).join("") : '<div class="empty">暂无来源记录</div>';
  }catch(error){
    $("sourceGrid").innerHTML = '<div class="empty">来源数据暂时无法加载</div>';
  }
}

async function checkHealth(){
  const badge = $("healthBadge");
  try{
    const data = await getJSON("/api/health");
    badge.textContent = data.ok ? "演示数据连接正常" : "演示数据异常";
    badge.className = "health " + (data.ok ? "ok" : "bad");
  }catch(error){
    badge.textContent = "数据连接失败";
    badge.className = "health bad";
  }
}

$("searchButton").addEventListener("click",loadEvents);
$("keywordInput").addEventListener("keydown",event => { if(event.key === "Enter") loadEvents(); });
$("clearFilters").addEventListener("click",()=>{
  $("keywordInput").value="";$("categorySelect").value="";$("sourceSelect").value="";loadEvents();
});
document.querySelector(".dialog-close").addEventListener("click",()=>$("eventDialog").close());
$("eventDialog").addEventListener("click",event=>{if(event.target===$("eventDialog")) $("eventDialog").close();});

checkHealth();
loadDashboard();
loadEvents();
loadSources();
