"""템플릿 __DATA__ 자리에 data.json 을 밀어넣어 standalone.html 을 만든다.

결과물은 서버도 인터넷도 없이 더블클릭만 하면 열린다.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tpl = (ROOT / "frontend" / "_standalone_template.html").read_text(encoding="utf-8")
data = (ROOT / "frontend" / "data.json").read_text(encoding="utf-8")

# 데이터 안에 </script> 가 섞여있으면 스크립트가 거기서 끊긴다
data = data.replace("</", "<\\/")
html = tpl.replace("__DATA__", data)

dest = ROOT / "frontend" / "standalone.html"
dest.write_text(html, encoding="utf-8")
kb = len(html.encode("utf-8")) / 1024
print(f"[build] {dest}  ({kb:.0f} KB)")
