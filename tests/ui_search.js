// Headless check of the cross-task search page and the job-page hit
// highlighting (including the stale-hit prompt).  Requires a live server.
// Usage: node tests/ui_search.js <baseUrl>
const { JSDOM } = require("jsdom");

const base = process.argv[2] || "http://127.0.0.1:5057";
const CSV = `框号,文字,x1,y1,x2,y2
1,白砂,10,10,50,40
2,糖,50,10,90,40
3,一级,110,10,190,40
4,5.00,210,10,290,40
5,跨格,90,10,110,40
`;

function assert(condition, message) {
  if (!condition) throw new Error("FAIL: " + message);
  console.log("ok - " + message);
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function upload() {
  const form = new FormData();
  form.append("file", new Blob([CSV], { type: "text/csv" }), "page.csv");
  const response = await fetch(`${base}/upload`, {
    method: "POST", body: form, redirect: "manual",
  });
  const jobId = response.headers.get("location").split("/").pop();
  await fetch(`${base}/api/jobs/${jobId}/lines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      horizontal: [0, 100, 200], vertical: [0, 100, 200, 300],
    }),
  });
  return jobId;
}

async function open(url) {
  const page = await fetch(`${base}${url}`).then((r) => r.text());
  const dom = new JSDOM(page, {
    url: `${base}${url}`,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.fetch = (url2, options) => fetch(new URL(url2, base), options);
  window.alert = (message) => { throw new Error("UI alert: " + message); };
  await new Promise((resolve) => window.addEventListener("load", resolve));
  await wait(400);
  return window;
}

(async () => {
  const jobId = await upload();

  // The search page finds the word spanning two boxes of one cell and links
  // each hit to the job page with its hit parameters.
  const searchHtml = await fetch(
    `${base}/search?q=${encodeURIComponent("白砂糖")}`).then((r) => r.text());
  assert(searchHtml.includes("<mark>白砂糖</mark>"),
    "search page highlights the match in context");
  assert(searchHtml.includes("第 1 行 · 品名"), "search page shows row and column");
  const linkRe = new RegExp('href="(/jobs/' + jobId + '\\?q=[^"]+)"');
  const linkMatch = searchHtml.match(linkRe);
  assert(linkMatch, "search page links to the job page with hit parameters");
  const hitUrl = linkMatch[1].replace(/&amp;/g, "&");
  assert(hitUrl.includes("entry=") && hitUrl.includes("offset="),
    "hit link carries entry and offset");

  // The job page highlights exactly the boxes participating in the hit.
  let win = await open(hitUrl);
  let doc = win.document;
  const highlighted = doc.querySelectorAll("#boxes g.box.hit");
  assert(highlighted.length === 2,
    `highlights the ${highlighted.length} boxes of the cross-box hit`);
  let notice = doc.getElementById("search-notice");
  assert(notice && !notice.hidden, "shows the search notice");
  assert(notice.textContent.includes("白砂糖"), "notice mentions the query");
  assert(!notice.classList.contains("bad"), "notice is not an error");
  win.close();

  // A pending box matched as a single box highlights just itself.
  const pendingHtml = await fetch(
    `${base}/search?q=${encodeURIComponent("跨格")}`).then((r) => r.text());
  assert(pendingHtml.includes("未归位 · 待核"),
    "search page marks the pending box as unplaced");
  const pendingLink = pendingHtml.match(linkRe)[1].replace(/&amp;/g, "&");
  win = await open(pendingLink);
  doc = win.document;
  assert(doc.querySelectorAll("#boxes g.box.hit").length === 1,
    "single-box hit highlights one box");
  win.close();

  // Reshape the grid so the recorded cell hit becomes invalid.
  await fetch(`${base}/api/jobs/${jobId}/lines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      horizontal: [0, 50, 100, 200], vertical: [0, 100, 200, 300],
    }),
  });

  // The same link now reports the hit as stale instead of highlighting.
  win = await open(hitUrl);
  doc = win.document;
  notice = doc.getElementById("search-notice");
  assert(notice && !notice.hidden, "stale hit still shows a notice");
  assert(notice.textContent.includes("已失效"), "notice asks to search again");
  assert(notice.classList.contains("bad"), "stale notice is marked as an error");
  assert(doc.querySelectorAll("#boxes g.box.hit").length === 0,
    "no box highlighted for a stale hit");
  win.close();

  console.log("done");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
