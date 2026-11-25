// The program selector of the desktop client: type, arrow keys, Enter opens the program's window.
// The list is fixed for the spike; with the plugin contract it will come from /api/commands.
const PROGRAMS = [
  { key: "chat", name: "Chat", words: "chat posteingang inbox" },
  { key: "research", name: "Recherche", words: "research recherche web wissen" },
  { key: "dev", name: "Entwicklung", words: "dev entwicklung code repo werkstatt" },
  { key: "project", name: "Projekte", words: "project projekte aufgaben" },
  { key: "admin", name: "Verwaltung", words: "admin verwaltung maschinen modelle" },
  { key: "files", name: "Dateien", words: "files dateien ablage" },
  { key: "incognito", name: "Inkognito", words: "incognito inkognito privat" },
  { key: "settings", name: "Einstellungen", words: "settings einstellungen persona anweisungen" },
];
const invoke = (cmd, args) => window.__TAURI__.core.invoke(cmd, args);
const q = document.getElementById("q");
const list = document.getElementById("list");
let shown = PROGRAMS;
let at = 0;

function draw() {
  const term = q.value.trim().toLowerCase();
  shown = PROGRAMS.filter((p) => !term || p.name.toLowerCase().includes(term) || p.words.includes(term));
  at = Math.min(at, Math.max(shown.length - 1, 0));
  list.replaceChildren(
    ...shown.map((p, i) => {
      const li = document.createElement("li");
      li.textContent = p.name;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === at));
      li.onclick = () => open(p.key);
      return li;
    }),
  );
}

async function open(key) {
  try {
    await invoke("open_program", { key });
    q.value = "";
    at = 0;
    draw();
  } catch (e) {
    if (String(e).includes("hub")) return setup(String(e));
    alert(String(e));
  }
}

async function setup(message) {
  document.getElementById("pick").hidden = true;
  document.getElementById("setup").hidden = false;
  document.getElementById("hub").value = await invoke("get_hub");
  document.getElementById("err").textContent = message || "";
  document.getElementById("hub").focus();
}

document.getElementById("setup").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await invoke("set_hub", { hub: document.getElementById("hub").value });
    document.getElementById("setup").hidden = true;
    document.getElementById("pick").hidden = false;
    q.focus();
  } catch (err) {
    document.getElementById("err").textContent = String(err);
  }
};
document.getElementById("cfg").onclick = () => setup("");
q.oninput = () => {
  at = 0;
  draw();
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") return invoke("hide_selector");
  if (document.getElementById("pick").hidden) return;
  if (e.key === "ArrowDown") at = Math.min(at + 1, shown.length - 1);
  else if (e.key === "ArrowUp") at = Math.max(at - 1, 0);
  else if (e.key === "Enter" && shown[at]) return open(shown[at].key);
  else return;
  e.preventDefault();
  draw();
});
window.topcoatSelectorShown = () => {
  q.value = "";
  at = 0;
  draw();
  q.focus();
};
invoke("get_hub").then((hub) => (hub ? window.topcoatSelectorShown() : setup("")));
