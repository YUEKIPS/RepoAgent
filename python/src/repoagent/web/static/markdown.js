/* Marked负责GFM语法，本文只将受控token转换为DOM；不解析任意HTML。
 * 输入为模型正文，返回展示节点；源字符串仍单独保存，可供复制/导出核对。
 */
"use strict";
(function (root) {
  function element(tag, text) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    return item;
  }
  function decode(text) {
    const names = {amp:"&",lt:"<",gt:">",quot:'"',apos:"'",nbsp:"\u00a0"};
    return String(text || "").replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (raw, name) => {
      if (name[0] !== "#") return names[name.toLowerCase()] || raw;
      const n = name[1].toLowerCase() === "x" ? parseInt(name.slice(2),16) : Number(name.slice(1));
      return n > 0 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) ? String.fromCodePoint(n) : "\ufffd";
    });
  }
  function safeLink(value) {
    if (!/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f]/.test(value)) return null;
    try { const url = new URL(value); return ["https:","http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch { return null; }
  }
  function render(text) {
    const body = element("div"); body.className = "markdown-body";
    if (!root.marked || text.length > 200000) { body.append(element("pre",text)); return body; }
    let count = 0;
    function append(parent, tokens, depth = 0) {
      if (depth > 32) throw new Error("Markdown nesting limit");
      for (const token of tokens || []) {
        if (++count > 30000) throw new Error("Markdown node limit");
        let child;
        const nested = (target, value = token.tokens) => append(target,value,depth+1);
        switch (token.type) {
          case "space": continue;
          case "heading": child=element(`h${Math.max(1,Math.min(6,token.depth))}`); nested(child); break;
          case "paragraph": child=element("p"); nested(child); break;
          case "text":
            if (token.tokens) { nested(parent); continue; }
            child=document.createTextNode(decode(token.text)); break;
          case "escape": child=document.createTextNode(token.text); break;
          case "strong": case "em": case "del": child=element(token.type); nested(child); break;
          case "codespan": child=element("code",token.text); break;
          case "br": child=element("br"); break;
          case "hr": child=element("hr"); break;
          case "blockquote": child=element("blockquote"); nested(child); break;
          case "code": {
            child=element("div");child.className="code-block";
            const pre=element("pre"), code=element("code",token.text), copy=element("button","复制代码");
            const toolbar=element("div"), language=element("span",(token.lang || "代码").split(/\s/)[0]);
            toolbar.className="code-toolbar";language.className="code-language";
            pre.append(code);copy.type="button";copy.className="copy-code";
            copy.addEventListener("click",async event=>{event.stopPropagation();try {await navigator.clipboard.writeText(token.text);copy.textContent="已复制";} catch {copy.textContent="请选中代码复制";}});
            toolbar.append(language,copy);child.append(toolbar,pre);break;
          }
          case "list": {
            child=element(token.ordered ? "ol" : "ul");
            if (token.ordered && Number.isSafeInteger(token.start)) child.start=token.start;
            for (const item of token.items) {const li=element("li");if(item.task) li.append(document.createTextNode(item.checked ? "☑ " : "☐ "));nested(li,item.tokens);child.append(li);}break;
          }
          case "table": {
            child=element("div");child.className="table-scroll";
            const table=element("table"),head=element("thead"),row=element("tr"),rows=element("tbody");
            function cell(value,tag,index) {const td=element(tag);if(["left","center","right"].includes(token.align[index])) td.className=`align-${token.align[index]}`;nested(td,value.tokens);return td;}
            token.header.forEach((value,index)=>row.append(cell(value,"th",index)));head.append(row);
            for(const values of token.rows) {const tr=element("tr");values.forEach((value,index)=>tr.append(cell(value,"td",index)));rows.append(tr);}
            table.append(head,rows);child.append(table);break;
          }
          case "link": {
            const href=safeLink(decode(token.href));child=element(href ? "a" : "span");
            if(href) {child.href=href;child.target="_blank";child.rel="noopener noreferrer";}
            nested(child);break;
          }
          case "image": child=element("span",decode(token.text));break;
          // 原始HTML只作为字面文本呈现；包括script、事件属性及SVG。
          default: child=document.createTextNode(token.raw || token.text || "");
        }
        parent.append(child);
      }
    }
    try {append(body,root.marked.lexer(text,{gfm:true,breaks:false}));}
    catch {body.replaceChildren(element("pre",text));}
    return body;
  }
  root.RepoMarkdown = {render,safeLink};
})(globalThis);
