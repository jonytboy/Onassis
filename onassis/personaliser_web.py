"""The Personaliser web app — what the buyer sees after buying on Etsy.

``/make/<product>``: enter the Etsy order number → unlocked → fill in the details
(or upload a photo) → live preview (Tier 1) / three variations to pick from
(Tier 2) → finish → instant high-resolution download. No human touches an order.

Photos arrive as base64 JSON (no multipart dependency). Sessions persist in the
DB so a refresh or a return visit still finds the finished file.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)

from onassis.personaliser import (PRODUCTS, geocode, render_tier1,
                                  transform_photo, upscale_for_print,
                                  verify_order, watermark)


def build_personaliser_router(config: Any, db: Any) -> APIRouter:
    router = APIRouter(prefix="/make", tags=["personaliser"])
    pcfg = dict(getattr(config, "personaliser", None) or {})
    out_root = Path(pcfg.get("out_dir", "exports/personaliser"))
    render_px = int(pcfg.get("render_px", 2400))

    def _session(token: str | None, key: str) -> dict[str, Any] | None:
        s = db.get_personaliser_session(token or "")
        return s if s and s.get("product") == key else None

    def _err(msg: str, code: int = 400) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=code)

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        items = "".join(
            f'<li><a href="/make/{p.key}">{p.name}</a> — {p.blurb}</li>'
            for p in PRODUCTS.values())
        return HTMLResponse(_INDEX.replace("__ITEMS__", items))

    @router.get("/{key}", response_class=HTMLResponse, include_in_schema=False)
    def page(key: str) -> HTMLResponse:
        p = PRODUCTS.get(key)
        if not p:
            return HTMLResponse("<p>Unknown product.</p>", status_code=404)
        spec = {"key": p.key, "name": p.name, "tier": p.tier, "blurb": p.blurb,
                "fields": p.fields, "styles": [{"key": s["key"], "label": s["label"]}
                                               for s in p.styles]}
        return HTMLResponse(_PAGE.replace("__SPEC__", json.dumps(spec)))

    @router.get("/api/geocode")
    def api_geocode(q: str) -> JSONResponse:
        hit = geocode(q)
        return JSONResponse({"ok": bool(hit), **(hit or {})})

    @router.post("/{key}/unlock")
    async def unlock(key: str, request: Request):
        if key not in PRODUCTS:
            return _err("Unknown product.", 404)
        body = await request.json()
        ok, why = verify_order(config, db, str(body.get("order_ref") or ""))
        if not ok:
            return _err(why, 403)
        token = secrets.token_urlsafe(18)
        raw = str(body.get("order_ref") or "")
        # Etsy receipt ids are stored as bare digits so a print order can be
        # matched back to this session by its receipt id; demo codes as typed.
        order_ref = "".join(ch for ch in raw if ch.isdigit()) if why == "etsy" else raw.strip()
        db.insert_personaliser_session({"token": token, "product": key,
                                        "order_ref": order_ref, "status": "unlocked"})
        return JSONResponse({"token": token})

    @router.post("/{key}/preview")
    async def preview(key: str, request: Request):
        p = PRODUCTS.get(key)
        body = await request.json()
        s = _session(body.get("token"), key)
        if not p or not s:
            return _err("Please enter your order number first.", 403)
        fields = dict(body.get("fields") or {})
        if p.tier == 1:
            try:
                img = render_tier1(key, fields, size=700, preview=True)
            except Exception as exc:
                return _err(f"Please check your details ({exc}).")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            buf.seek(0)
            db.update_personaliser_session(s["token"], status="previewed", fields=fields)
            return StreamingResponse(buf, media_type="image/png")
        # Tier 2: photo → N variations, saved so "finish" can pick one.
        photo_b64 = body.get("photo") or ""
        if "," in photo_b64:
            photo_b64 = photo_b64.split(",", 1)[1]
        try:
            photo = base64.b64decode(photo_b64)
        except Exception:
            return _err("Please upload a photo.")
        if len(photo) < 1000:
            return _err("Please upload a photo.")
        try:
            variations = transform_photo(config, photo, key, str(fields.get("style") or ""))
        except Exception as exc:
            return _err(f"We couldn't create your portrait right now: {exc}", 503)
        vdir = out_root / s["token"]
        vdir.mkdir(parents=True, exist_ok=True)
        thumbs = []
        from PIL import Image
        for i, vb in enumerate(variations):
            (vdir / f"var{i}.png").write_bytes(vb)
            im = Image.open(io.BytesIO(vb)).convert("RGB")
            im.thumbnail((640, 640))
            watermark(im)
            b = io.BytesIO()
            im.save(b, "JPEG", quality=85)
            thumbs.append("data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode())
        db.update_personaliser_session(s["token"], status="previewed",
                                       fields={**fields, "variations": len(variations)})
        return JSONResponse({"variations": thumbs})

    @router.post("/{key}/finalize")
    async def finalize(key: str, request: Request):
        p = PRODUCTS.get(key)
        body = await request.json()
        s = _session(body.get("token"), key)
        if not p or not s:
            return _err("Please enter your order number first.", 403)
        out_root.mkdir(parents=True, exist_ok=True)
        fp = out_root / f"{s['token']}.png"
        if p.tier == 1:
            fields = dict(body.get("fields") or {})
            try:
                img = render_tier1(key, fields, size=render_px)
            except Exception as exc:
                return _err(f"Please check your details ({exc}).")
            img.save(fp, "PNG", dpi=(300, 300))
        else:
            choice = int(body.get("choice", 0))
            src = out_root / s["token"] / f"var{choice}.png"
            if not src.exists():
                return _err("Pick one of your variations first.")
            fp.write_bytes(upscale_for_print(src.read_bytes()))
        db.update_personaliser_session(s["token"], status="done", file_path=str(fp))
        return JSONResponse({"download": f"/make/download/{s['token']}"})

    @router.get("/download/{token}")
    def download(token: str):
        s = db.get_personaliser_session(token)
        if not s or not s.get("file_path") or not Path(s["file_path"]).exists():
            return _err("Not ready yet.", 404)
        name = f"{s['product']}.png"
        return FileResponse(s["file_path"], media_type="image/png", filename=name)

    return router


_CSS = """
 body{margin:0;background:#f4f1e9;color:#12203a;font-family:Georgia,'Times New Roman',serif}
 .wrap{max-width:1040px;margin:0 auto;padding:30px 20px;display:grid;
   grid-template-columns:1fr 1fr;gap:34px}
 @media(max-width:820px){.wrap{grid-template-columns:1fr}}
 h1{font-size:28px;margin:0 0 6px}.sub{color:#5b6478;margin:0 0 18px;line-height:1.4}
 label{display:block;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
   color:#5b6478;margin:13px 0 5px}
 input,select{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cfc9bb;
   border-radius:8px;background:#fff;font-size:15px;font-family:inherit}
 .btn{margin-top:18px;width:100%;padding:13px;border:0;border-radius:10px;
   background:#12203a;color:#f4f1e9;font-size:16px;font-family:inherit;cursor:pointer}
 .btn:disabled{opacity:.5}.btn.alt{background:#b89254}
 .card{background:#fff;border:1px solid #e7e1d3;border-radius:12px;padding:14px}
 .preview img{max-width:100%;border-radius:6px}.hint{font-size:12px;color:#8a8574;margin-top:6px}
 .err{color:#b34a4a;font-size:13px;min-height:16px;margin-top:8px}
 .vars{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
 .vars img{width:100%;border-radius:6px;border:3px solid transparent;cursor:pointer}
 .vars img.on{border-color:#b89254}
 .step{font-size:12px;color:#b89254;letter-spacing:.08em;text-transform:uppercase;margin-top:22px}
"""

_INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>Onassis Studio</title>
<style>""" + _CSS + """ ul{line-height:1.8}</style></head><body>
<div class="wrap" style="grid-template-columns:1fr"><div>
<h1>Onassis Studio</h1><p class="sub">Personalise the product you bought. Pick it below:</p>
<ul>__ITEMS__</ul></div></div></body></html>"""

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Personalise — Onassis Studio</title><style>""" + _CSS + """</style></head><body>
<div class="wrap">
 <div>
  <h1 id="name"></h1><p class="sub" id="blurb"></p>
  <div class="step">Step 1 — your order</div>
  <label>Etsy order number</label>
  <input id="order" placeholder="e.g. 3412345678">
  <button class="btn" id="unlock">Unlock</button>
  <div class="err" id="err1"></div>
  <div id="form" style="display:none">
   <div class="step" id="step2">Step 2 — your details</div>
   <div id="fields"></div>
   <div class="hint" id="geo"></div>
   <div class="err" id="err2"></div>
   <button class="btn alt" id="gen" style="display:none">Create my 3 variations</button>
   <button class="btn" id="finish" disabled>Finish &amp; download</button>
  </div>
 </div>
 <div class="card preview">
  <img id="img" alt="">
  <div class="vars" id="vars"></div>
  <div class="hint" id="phint">Your preview appears here.</div>
 </div>
</div>
<script>
const S=__SPEC__;const $=id=>document.getElementById(id);
$('name').textContent=S.name;$('blurb').textContent=S.blurb;
let token=null,lat=null,lon=null,t=null,choice=null,photo=null;
function debounce(f){clearTimeout(t);t=setTimeout(f,450);}
function buildForm(){
  const F=$('fields');F.innerHTML='';
  if(S.tier===1){
    S.fields.forEach(f=>{const l=document.createElement('label');l.textContent=f.label;
      const i=document.createElement('input');i.id='f_'+f.key;i.type=f.type||'text';
      i.placeholder=f.placeholder||'';if(f.default)i.value=f.default;
      i.addEventListener('input',()=>{ if(f.key==='place'){debounce(geocode);} else debounce(preview);});
      F.appendChild(l);F.appendChild(i);});
  }else{
    const l=document.createElement('label');l.textContent='Your photo';const i=document.createElement('input');
    i.type='file';i.accept='image/*';i.addEventListener('change',e=>{const r=new FileReader();
      r.onload=()=>{photo=r.result;$('gen').style.display='block';};r.readAsDataURL(e.target.files[0]);});
    F.appendChild(l);F.appendChild(i);
    const l2=document.createElement('label');l2.textContent='Style';const s=document.createElement('select');s.id='f_style';
    S.styles.forEach(st=>{const o=document.createElement('option');o.value=st.key;o.textContent=st.label;s.appendChild(o);});
    F.appendChild(l2);F.appendChild(s);
  }
}
function fields(){const o={};if(S.tier===1){S.fields.forEach(f=>o[f.key]=$('f_'+f.key).value);
  if(lat!==null){o.lat=lat;o.lon=lon;}}else{o.style=$('f_style').value;}return o;}
async function geocode(){const q=$('f_place').value.trim();if(!q)return;$('geo').textContent='finding place…';
  const r=await fetch('/make/api/geocode?q='+encodeURIComponent(q));const d=await r.json();
  if(d.ok){lat=d.lat;lon=d.lon;$('geo').textContent=d.name.slice(0,70);preview();}
  else{$('geo').textContent='Place not found — try adding the country.';}}
async function preview(){ if(!token)return; $('err2').textContent='';
  if(S.tier===1){ if(S.fields.some(f=>f.key==='place')&&lat===null)return;
   const r=await fetch('/make/'+S.key+'/preview',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({token,fields:fields()})});
   if(!r.ok){$('err2').textContent=(await r.json()).error||'error';return;}
   $('img').src=URL.createObjectURL(await r.blob());$('phint').textContent='Live preview — the finished file has no watermark.';
   $('finish').disabled=false;
  }else{ if(!photo){$('err2').textContent='Upload a photo first.';return;}
   $('gen').disabled=true;$('gen').textContent='Painting… (about a minute)';$('phint').textContent='Creating your 3 variations…';
   const r=await fetch('/make/'+S.key+'/preview',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({token,fields:fields(),photo})});
   const d=await r.json();$('gen').disabled=false;$('gen').textContent='Create my 3 variations';
   if(!r.ok){$('err2').textContent=d.error||'error';return;}
   const V=$('vars');V.innerHTML='';$('img').src='';
   d.variations.forEach((src,i)=>{const im=document.createElement('img');im.src=src;
     im.onclick=()=>{choice=i;[...V.children].forEach(c=>c.classList.remove('on'));im.classList.add('on');$('finish').disabled=false;};
     V.appendChild(im);});
   $('phint').textContent='Tap your favourite, then Finish.';}
}
$('unlock').onclick=async()=>{$('err1').textContent='';
  const r=await fetch('/make/'+S.key+'/unlock',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({order_ref:$('order').value})});const d=await r.json();
  if(!r.ok){$('err1').textContent=d.error||'error';return;}
  token=d.token;$('form').style.display='block';$('unlock').textContent='Unlocked ✓';$('unlock').disabled=true;
  buildForm(); if(S.tier===1&&S.fields.some(f=>f.key==='place')){ if($('f_place').value) geocode(); } };
$('gen').onclick=preview;
$('finish').onclick=async()=>{$('finish').disabled=true;$('finish').textContent='Rendering…';
  const body={token};if(S.tier===1)body.fields=fields();else body.choice=choice;
  const r=await fetch('/make/'+S.key+'/finalize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();if(!r.ok){$('err2').textContent=d.error||'error';$('finish').disabled=false;$('finish').textContent='Finish & download';return;}
  location.href=d.download;};
</script></body></html>"""
