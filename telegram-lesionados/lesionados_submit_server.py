#!/usr/bin/env python3
import json, re, os, sys
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen
CONFIG='/opt/telegram-lesionados/config.json'
CENTERS=['Getafe','Parla','Las Rosas']
def cfg(): return json.load(open(CONFIG))
def clean(s, n=1200): return re.sub(r'\s+',' ',str(s or '').strip())[:n]
def token(path):
    for line in Path(path).read_text().splitlines():
        if line.startswith('TELEGRAM_BOT_TOKEN='): return line.split('=',1)[1].strip().strip('"')
    raise RuntimeError('TELEGRAM_BOT_TOKEN not found')
def tg(c, method, data):
    req=Request(f"https://api.telegram.org/bot{token(c['telegramEnv'])}/{method}", data=urlencode(data).encode(), method='POST')
    with urlopen(req, timeout=20) as r: out=json.loads(r.read().decode())
    if not out.get('ok'): raise RuntimeError(out.get('description') or str(out))
    return out['result']
def message(d):
    seg='Sí' if d.get('seguimiento') else 'No'
    eti='Sí' if d.get('etiqueta') else 'No'
    parts=[f"🟧 LESIONADO — {d['centro']}", f"Nombre y apellidos: {d['nombre']}", f"Lesión: {d['lesion']}", f"Seguimiento: {seg}", f"Etiqueta: {eti}"]
    if d.get('notas'): parts.append(f"Notas: {d['notas']}")
    parts.append('Origen: formulario lesionados')
    return '\n'.join(parts)
def append_local(c, d, res):
    p=Path(c['localJsonl']); p.parent.mkdir(parents=True, exist_ok=True)
    rec={**d,'created_at':datetime.now().isoformat(timespec='seconds'),'telegram_message_id':res.get('message_id'),'telegram_chat_id':res.get('chat',{}).get('id')}
    with p.open('a') as f: f.write(json.dumps(rec,ensure_ascii=False)+'\n')
    return rec
class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith('/health'): return self._send(200, {'ok':True})
        return self._send(404, {'ok':False})
    def do_POST(self):
        if not self.path.startswith('/api/lesionados'): return self._send(404, {'ok':False})
        try:
            raw=self.rfile.read(min(int(self.headers.get('content-length','0')), 20000)); body=json.loads(raw.decode())
            d={
              'centro':clean(body.get('centro'),50), 'nombre':clean(body.get('nombre'),140), 'lesion':clean(body.get('lesion'),1000),
              'seguimiento':bool(body.get('seguimiento')), 'etiqueta':bool(body.get('etiqueta')), 'notas':clean(body.get('notas'),1200)
            }
            if d['centro'] not in CENTERS: raise ValueError('Centro inválido')
            if not d['nombre'] or not d['lesion']: raise ValueError('Falta nombre o lesión')
            c=cfg(); thread=c.get('threads',{}).get(d['centro'])
            if not thread: raise ValueError(f"Falta configurar el tema Lesionados de {d['centro']}")
            chat={v:k for k,v in c['allowedChats'].items()}.get(d['centro'])
            payload={'chat_id':chat,'message_thread_id':str(thread),'text':message(d),'disable_web_page_preview':'true'}
            res=tg(c,'sendMessage',payload); rec=append_local(c,d,res)
            sheets_ok = False
            try:
                from sheets_sync import append_record
                sheets_ok = bool(append_record(rec, c))
            except Exception as sheets_error:
                print(f'[lesionados] sheets append failed: {sheets_error}', file=sys.stderr)
            self._send(200, {'ok':True,'centro':d['centro'],'messageId':res.get('message_id'),'thread':thread,'sheets':sheets_ok})
        except Exception as e: self._send(400, {'ok':False,'error':str(e)})
if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1', int(cfg().get('port',5096))), H).serve_forever()
