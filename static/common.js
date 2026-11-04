// Shared helpers
const $ = (s, el = document) => el.querySelector(s);

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const msg = data && (typeof data.detail === "string" ? data.detail : "Something went wrong");
    const err = new Error(msg || `Error ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// Polls this browser created: { id: { token, question, created } }
const MyPolls = {
  all() {
    try { return JSON.parse(localStorage.getItem("myPolls") || "{}"); } catch { return {}; }
  },
  get(id) { return this.all()[id]; },
  save(id, info) {
    try {
      const all = this.all();
      all[id] = info;
      localStorage.setItem("myPolls", JSON.stringify(all));
    } catch {}
  },
};

function toast(msg) {
  let t = $(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 1600);
}

async function copy(text, label = "Copied!") {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const i = document.createElement("textarea");
    i.value = text; document.body.appendChild(i); i.select();
    document.execCommand("copy"); i.remove();
  }
  toast(label);
}

function renderResults(container, poll) {
  const max = Math.max(...poll.counts);
  const rows = poll.options.map((opt, i) => {
    const n = poll.counts[i];
    const pct = poll.total ? Math.round((n / poll.total) * 100) : 0;
    const lead = poll.total && n === max;
    return `<div class="res${lead ? " lead" : ""}">
      <div class="bar" style="width:${pct}%"></div>
      <div class="line"><span>${esc(opt)}${poll.voted === i ? '<span class="mine">YOUR VOTE</span>' : ""}</span><span class="pct">${pct}%</span></div>
      <div class="cnt">${n.toLocaleString()} vote${n === 1 ? "" : "s"}</div>
    </div>`;
  });
  // Update in place so bar widths animate.
  if (container.children.length === rows.length) {
    rows.forEach((html, i) => {
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      const fresh = tmp.firstElementChild, old = container.children[i];
      old.className = fresh.className;
      old.querySelector(".bar").style.width = fresh.querySelector(".bar").style.width;
      old.querySelector(".line").innerHTML = fresh.querySelector(".line").innerHTML;
      old.querySelector(".cnt").innerHTML = fresh.querySelector(".cnt").innerHTML;
    });
  } else {
    container.innerHTML = rows.join("");
  }
}
