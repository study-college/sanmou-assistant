"""Render a designed, self-contained offline guide; Markdown is a build dependency."""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path

import markdown


def slug(text, separator):
    return re.sub(r"[^\w\u4e00-\u9fff]+", separator, text, flags=re.UNICODE).strip(separator).lower()


def render_document(document: Path, root: Path, version: str, *, agent=False):
    raw = document.read_text(encoding="utf-8")
    raw = re.sub(r"^#\s+[^\n]+\n", "", raw, count=1)
    engine = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"],
                               extension_configs={"toc": {"slugify": slug, "toc_depth": "2-2"}})
    content = engine.convert(raw)
    content = re.sub(r"<table>(.*?)</table>", r'<div class="table-scroll" tabindex="0">\g<0></div>', content, flags=re.S)
    def embed(match):
        path = (document.parent / html.unescape(match[1])).resolve()
        if not path.is_relative_to((root / "docs").resolve()) or not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".svg"):
            return match[0]
        mime = {".png":"image/png", ".jpg":"image/jpeg", ".svg":"image/svg+xml"}[path.suffix.lower()]
        return 'src="data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode() + '"'
    content = re.sub(r'src="([^"]+)"', embed, content)
    content = content.replace('href="AGENT-GUIDE.md"', 'href="docs/AGENT-GUIDE.html"' if not agent else 'href="AGENT-GUIDE.html"')
    nav = "".join(f'<a href="#{html.escape(t["id"],quote=True)}">{html.escape(re.sub("<[^>]+>","",t["name"]))}</a>' for t in engine.toc_tokens)
    title = "Agent 操作与维护手册" if agent else "三谋助手，先从这里开始。"
    lead = "操作前先确认用户授权、所选实例、账号和当前记录。所有输入由同一个 Worker 串行执行。" if agent else "先连上自己的游戏，再开启需要的任务。程序、源码和使用说明都在这个文件夹里；这份指南断网也能完整阅读。"
    first = engine.toc_tokens[0]["id"] if engine.toc_tokens else "content"
    css = (root / "docs" / "guide.css").read_text(encoding="utf-8")
    role_link = '../先读我.html' if agent else 'docs/AGENT-GUIDE.html'
    role_label = '返回使用指南' if agent else '打开 Agent 手册'
    ai_prompt = "请先读取解压目录中的 source/AGENTS.md 和 docs/AGENT-GUIDE.md，再检查程序版本与当前配置。使用我的 MuMu、实例编号、游戏渠道和账号；先只监控核对，不直接沿用作者的账号配置。"
    steps = '<div class="steps">' + ''.join(f'<div class="step"><div class="num">0{i}</div><h3>{a}</h3><p>{b}</p></div>' for i,(a,b) in enumerate([
        ("完整解压", "保持文件夹完整，先找到根目录的启动器。"),
        ("打开助手", "双击启动助手.cmd，普通使用无需 Python。"),
        ("连接自己的游戏", "发现 MuMu 实例，核对渠道、昵称和主城。"),
        ("先监控，再执行", "确认队伍读数后，开启你选定的任务。")],1)) + '</div>'
    extras = '' if agent else f'''<div class="quick-title">四步开始使用</div>{steps}<div class="roles"><div class="role"><small>普通使用</small><h3>有程序，就能开始。</h3><p>无需 AI 或 Python。配置自己的模拟器和账号，所有任务都由你选择开启。</p><a href="#解压与首次配置">查看首次配置步骤 →</a></div><div class="role"><small>AI 协助</small><h3>源码和手册，也在包里。</h3><p>让 Agent 先读操作约定，再帮你设置、查看诊断或修改源码。</p><button class="copy-ai" id="copy-ai" data-copy="{html.escape(ai_prompt,quote=True)}">复制给 Agent 的说明 →</button><div class="result-note" id="copy-result" role="status" aria-live="polite"></div></div></div>'''
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>{title} · v{version}</title><style>{css}</style></head>
<body><a class="skip" href="#content">跳到正文</a><header class="top"><div class="top-inner"><a class="brand" href="#"><span class="mark">谋</span>三谋助手<span class="secondary-label" style="font-weight:400;letter-spacing:0;font-size:12px;color:#65748b">使用与操作指南</span></a><span class="version">v{version} · 离线版</span></div><div class="progress"><span id="reading-progress"></span></div></header>
<div class="shell"><aside class="side" aria-label="文档目录"><p class="label">CONTENTS / 阅读导航</p><input class="search" id="guide-search" placeholder="查找实例、铺路、日志…" aria-label="搜索说明内容"><nav>{nav}</nav><div class="side-note"><strong>每台电脑，使用自己的配置</strong>实例编号、渠道、主城和任务目标都由接收者设置。</div></aside><main class="main" id="content"><section class="hero"><div class="eyebrow">SANMOU ASSISTANT / START HERE</div><h1>{title}</h1><p>{lead}</p><div class="actions"><a class="action primary" href="#{first}">开始阅读 ↓</a><a class="action" href="{role_link}">{role_label} →</a></div><div class="chips"><span>程序 + 完整源码</span><span>一个实例，串行控制</span><span>本地日志与图片诊断</span></div></section>{extras}<details class="mobile-toc"><summary>展开阅读目录</summary><nav>{nav}</nav></details><article class="doc" id="guide-document">{content}</article><p class="empty" id="no-results">没有找到匹配内容。试试“实例”“铺路”或“诊断”。</p><footer class="footer"><span>三谋助手 v{version} · 完整分发版</span><span>所有样式与内容均在本地，不依赖外部字体或脚本</span></footer></main></div>
<script>
const bar=document.getElementById('reading-progress');addEventListener('scroll',()=>{{const h=document.documentElement.scrollHeight-innerHeight;bar.style.width=(h>0?scrollY/h*100:0)+'%'}},{{passive:true}});
const headings=[...document.querySelectorAll('.doc h2')];const groups=[];for(const h of headings){{const nodes=[h];let n=h.nextElementSibling;while(n&&n.tagName!=='H2'){{nodes.push(n);n=n.nextElementSibling}}groups.push(nodes)}}
document.getElementById('guide-search').addEventListener('input',e=>{{const q=e.target.value.trim().toLowerCase();let visible=0;groups.forEach(nodes=>{{const match=!q||nodes.some(n=>n.textContent.toLowerCase().includes(q));nodes.forEach(n=>n.hidden=!match);visible+=match}});document.getElementById('no-results').style.display=visible?'none':'block'}});
const links=[...document.querySelectorAll('.side nav a')];const observer=new IntersectionObserver(entries=>{{for(const e of entries)if(e.isIntersecting)links.forEach(a=>a.classList.toggle('active',decodeURIComponent(a.hash.slice(1))===e.target.id))}},{{rootMargin:'-10% 0px -75% 0px'}});headings.forEach(h=>observer.observe(h));
document.getElementById('copy-ai')?.addEventListener('click',async e=>{{const text=e.target.dataset.copy;try{{if(navigator.clipboard)await navigator.clipboard.writeText(text);else{{const t=document.createElement('textarea');t.value=text;document.body.append(t);t.select();document.execCommand('copy');t.remove()}}document.getElementById('copy-result').textContent='已复制，粘贴给你的 Agent 即可。'}}catch{{document.getElementById('copy-result').textContent=text}}}});
</script></body></html>'''


def write_guides(destination: Path, root: Path, version: str):
    (destination / "docs").mkdir(parents=True, exist_ok=True)
    (destination / "先读我.html").write_text(render_document(root / "docs" / "09-user-guide.md", root, version), encoding="utf-8")
    (destination / "docs" / "AGENT-GUIDE.html").write_text(render_document(root / "docs" / "AGENT-GUIDE.md", root, version, agent=True), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path,required=True);parser.add_argument("--version",default="0.5.0")
    args=parser.parse_args();write_guides(args.output,Path(__file__).resolve().parents[1],args.version)
