// Functions that run inside the page. Loaded by crau.browser.javascript, which splits this
// file on the "// == NAME" markers and replaces the /*FIND*/ placeholder with the FIND section.
//
// Targets accepted by every helper: a CSS selector (default), "text=Visible text" (clickable elements first) or
// "label=Field label" (form control by <label>, placeholder, aria-label or name).

// == FIND
const __find = (target) => {
  const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const best = (list) => list.find(shown) || list[0] || null;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  if (target.startsWith("text=")) {
    const wanted = norm(target.slice(5));
    const label = (el) => norm(el.innerText || el.textContent || el.value || el.getAttribute("aria-label"));
    const tags = "a,button,[role=button],input[type=submit],input[type=button],summary,label";
    const clickable = [...document.querySelectorAll(tags)];
    const exact = clickable.filter((el) => label(el) === wanted);
    if (exact.length) return best(exact);
    const partial = clickable.filter((el) => label(el).includes(wanted));
    if (partial.length) return best(partial);
    const leaves = [...document.querySelectorAll("body *")].filter((el) => !el.children.length);
    return best(leaves.filter((el) => label(el).includes(wanted)));
  }
  if (target.startsWith("label=")) {
    const wanted = norm(target.slice(6));
    const found = [];
    for (const label of document.querySelectorAll("label")) {
      if (!norm(label.innerText || label.textContent).includes(wanted)) continue;
      const control = label.control || (label.htmlFor && document.getElementById(label.htmlFor))
        || label.querySelector("input,textarea,select");
      if (control) found.push(control);
    }
    for (const el of document.querySelectorAll("input,textarea,select,[contenteditable]")) {
      const names = [el.getAttribute("placeholder"), el.getAttribute("aria-label"), el.getAttribute("name"), el.id];
      if (names.some((name) => norm(name).includes(wanted))) found.push(el);
    }
    return best(found);
  }
  return best([...document.querySelectorAll(target)]);
};

// == EXISTS
(target) => {
  /*FIND*/
  return __find(target) !== null;
}

// == ELEMENT_BOX
async (target) => {
  /*FIND*/

  const el = __find(target);
  if (!el) return null;
  el.scrollIntoView({block: "center", inline: "center"});
  // scrolling settles asynchronously (Firefox): measure only after a frame, or 100 ms when frames are throttled
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 100);
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(timer); resolve(); }));
    }
  });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint ? document.elementFromPoint(x, y) : null;
  return {x, y, left: r.left + window.scrollX, top: r.top + window.scrollY, width: r.width, height: r.height,
          hit: Boolean(top && (top === el || el.contains(top) || top.contains(el)))};
}

// == CLICK
(target) => {
  /*FIND*/
  const el = __find(target);
  if (!el) return false;
  el.click();
  return true;
}

// == FOCUS
(target) => {
  /*FIND*/

  const el = __find(target);
  if (!el) return false;
  el.focus();
  if (typeof el.select === "function") el.select();
  return true;
}

// == FILL
(target, value) => {
  /*FIND*/

  const el = __find(target);
  if (!el) return false;
  el.focus();
  const fire = (type) => el.dispatchEvent(new Event(type, {bubbles: true}));
  if (el.type === "checkbox" || el.type === "radio") {
    const wanted = value === true || value === "true" || value === "on" || value === "1";
    if (el.checked !== wanted) el.click();
    return true;
  }
  if (el.tagName === "SELECT") {
    const option = [...el.options].find((o) => o.value === String(value) || o.text.trim() === String(value));
    if (!option) return false;
    el.value = option.value;
  } else if (el.isContentEditable) {
    el.textContent = String(value);
  } else {
    // frameworks that track the value (React) only notice writes made through the native setter
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, String(value)); else el.value = String(value);
  }
  fire("input");
  fire("change");
  return true;
}

// == SCROLL_STEP
() => {
  const before = window.scrollY;
  window.scrollBy(0, window.innerHeight);
  return {moved: window.scrollY !== before, height: document.documentElement.scrollHeight};
}

// == PAGE_SIZE
() => {
  const d = document.documentElement, b = document.body || d;
  return {width: Math.max(d.scrollWidth, b.scrollWidth, d.clientWidth),
          height: Math.max(d.scrollHeight, b.scrollHeight, d.clientHeight)};
}

// == TEXT
() => (document.body ? (document.body.innerText || document.body.textContent) : document.documentElement.textContent) || ''

// == HTML
() => (document.doctype ? new XMLSerializer().serializeToString(document.doctype) + '\n' : '') + document.documentElement.outerHTML

// == OUTLINE
() => {
  const cssPath = (el) => {
    if (el.id && document.querySelectorAll("#" + CSS.escape(el.id)).length === 1) return "#" + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      let part = el.tagName.toLowerCase();
      const unique = el.id && document.querySelectorAll("#" + CSS.escape(el.id)).length === 1;
      if (unique) { parts.unshift("#" + CSS.escape(el.id)); break; }
      const name = el.getAttribute("name");
      if (name && el.parentElement && el.parentElement.querySelectorAll(part + '[name="' + name + '"]').length === 1) {
        part += '[name="' + name + '"]';
      } else if (el.parentElement) {
        const same = [...el.parentElement.children].filter((c) => c.tagName === el.tagName);
        if (same.length > 1) part += ":nth-of-type(" + (same.indexOf(el) + 1) + ")";
      }
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(" > ");
  };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 120);
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "a") return "link";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (["checkbox", "radio"].includes(type)) return type;
      return "textbox";
    }
    return tag;
  };
  const nameOf = (el) => {
    const label = el.labels && el.labels[0] ? el.labels[0].innerText || el.labels[0].textContent : "";
    return clean(el.getAttribute("aria-label") || label || el.getAttribute("placeholder") || el.getAttribute("alt")
      || el.innerText || el.textContent || el.value || el.getAttribute("title") || el.getAttribute("name"));
  };
  const items = [];
  const query = "h1,h2,h3,h4,h5,h6,a[href],button,summary,input:not([type=hidden]),select,textarea,[role],"
    + "[contenteditable=true],iframe";
  // Lightpanda returns comma-separated selector matches grouped by selector: put them back in document order
  const elements = [...document.querySelectorAll(query)];
  if (typeof document.body.compareDocumentPosition === "function") {
    elements.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  }
  for (const el of elements) {
    const item = {role: roleOf(el), name: nameOf(el), selector: cssPath(el)};
    const tag = el.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) item.level = Number(tag[1]);
    if (tag === "a") item.href = el.href;
    if (tag === "iframe") item.src = el.src;
    if (tag === "input") item.type = (el.getAttribute("type") || "text").toLowerCase();
    if (["input", "textarea", "select"].includes(tag) && el.type !== "password") item.value = clean(el.value);
    if (el.type === "checkbox" || el.type === "radio") item.checked = el.checked;
    if (el.disabled) item.disabled = true;
    items.push(item);
  }
  return {url: location.href, title: document.title, items};
}
