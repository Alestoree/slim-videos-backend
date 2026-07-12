import os
import uuid
import shutil
import tempfile
import json
from datetime import datetime, timezone
from threading import Lock

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)  # Permite que tu página de GitHub Pages llame a este servidor

# Cookies de una cuenta real de YouTube (opcional, pero casi siempre necesaria).
# Se configuran en Render → Environment → YOUTUBE_COOKIES, NUNCA en el código ni en GitHub.
_cookies_content = os.environ.get('YOUTUBE_COOKIES', '').strip()
_COOKIES_FILE = None
if _cookies_content:
    _COOKIES_FILE = os.path.join(tempfile.gettempdir(), 'youtube_cookies.txt')
    with open(_COOKIES_FILE, 'w', encoding='utf-8') as f:
        f.write(_cookies_content + '\n')

COMMON_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web', 'ios'],
        }
    },
}
if _COOKIES_FILE:
    COMMON_OPTS['cookiefile'] = _COOKIES_FILE

# Contraseña del panel de historial. Puedes cambiarla sin tocar código:
# en Render → tu servicio → Environment → agrega ADMIN_PASSWORD con tu clave.
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'slim2026')
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'history.json')
HISTORY_MAX = 500
history_lock = Lock()


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history[-HISTORY_MAX:], f)


@app.route('/')
def home():
    return jsonify({'status': 'ok', 'message': 'Slim Videos backend corriendo'})


@app.route('/api/log', methods=['POST'])
def log_entry():
    data = request.get_json(force=True, silent=True) or {}
    entry = {
        'platform': data.get('platform', 'desconocido'),
        'url': data.get('url', ''),
        'title': data.get('title', 'Sin título'),
        'thumbnail': data.get('thumbnail', ''),
        'format': data.get('format', ''),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    with history_lock:
        history = load_history()
        history.append(entry)
        save_history(history)
    return jsonify({'ok': True})


@app.route('/api/history')
def get_history():
    password = request.args.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Contraseña incorrecta'}), 401
    with history_lock:
        history = load_history()
    return jsonify({'history': list(reversed(history))})


@app.route('/api/info')
def info():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Falta el parámetro url'}), 400

    opts = dict(COMMON_OPTS)
    opts['noplaylist'] = False  # permite detectar carruseles de Instagram con varios elementos

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({'error': f'No se pudo procesar el video: {str(e)}'}), 502

    entries = data.get('entries')
    if entries:
        entries = list(entries)
        items = []
        for idx, e in enumerate(entries, start=1):
            items.append({
                'index': idx,
                'title': e.get('title'),
                'thumbnail': e.get('thumbnail'),
                'is_video': e.get('vcodec') not in (None, 'none'),
            })
        return jsonify({
            'type': 'carousel',
            'title': data.get('title') or 'Publicación',
            'thumbnail': items[0]['thumbnail'] if items else None,
            'items': items,
        })

    # Calidades de video disponibles (mp4, con audio y video juntos o combinables)
    seen = set()
    qualities = []
    for f in data.get('formats', []):
        height = f.get('height')
        if not height or f.get('vcodec') == 'none':
            continue
        label = f'{height}p'
        if label not in seen:
            seen.add(label)
            qualities.append(label)
    qualities.sort(key=lambda q: int(q.replace('p', '')), reverse=True)

    return jsonify({
        'type': 'single',
        'title': data.get('title'),
        'thumbnail': data.get('thumbnail'),
        'duration': data.get('duration'),
        'uploader': data.get('uploader'),
        'uploader_url': data.get('uploader_url') or data.get('channel_url'),
        'uploader_avatar': data.get('channel_thumbnail') or data.get('uploader_avatar'),
        'channel_id': data.get('channel_id'),
        'qualities': qualities,
    })


@app.route('/api/channel_videos')
def channel_videos():
    channel_url = request.args.get('channel_url')
    if not channel_url:
        return jsonify({'error': 'Falta el parámetro channel_url'}), 400

    opts = dict(COMMON_OPTS)
    opts.update({'extract_flat': True, 'playlistend': 12})

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(channel_url, download=False)
    except Exception as e:
        return jsonify({'error': f'No se pudo obtener el canal: {str(e)}'}), 502

    entries = data.get('entries') or []
    videos = [{
        'id': e.get('id'),
        'title': e.get('title'),
        'thumbnail': e.get('thumbnails', [{}])[-1].get('url') if e.get('thumbnails') else f"https://img.youtube.com/vi/{e.get('id')}/hqdefault.jpg",
        'url': f"https://www.youtube.com/watch?v={e.get('id')}",
    } for e in entries if e.get('id')]

    return jsonify({'videos': videos})


@app.route('/api/download')
def download():
    url = request.args.get('url')
    quality = request.args.get('quality')       # ej "720p", o None para mejor disponible
    audio_only = request.args.get('audio') == '1'
    item_index = request.args.get('item_index')  # para un elemento específico de un carrusel

    if not url:
        return jsonify({'error': 'Falta el parámetro url'}), 400

    tmp_dir = tempfile.mkdtemp(prefix='slimvideos_')
    out_template = os.path.join(tmp_dir, f'{uuid.uuid4().hex}.%(ext)s')

    if audio_only:
        fmt = 'bestaudio/best'
    elif quality:
        height = quality.replace('p', '')
        fmt = f'bestvideo[height<={height}]+bestaudio/best[height<={height}]'
    else:
        fmt = 'bestvideo+bestaudio/best/best'

    ydl_opts = dict(COMMON_OPTS)
    ydl_opts.update({
        'outtmpl': out_template,
        'format': fmt,
        'merge_output_format': 'mp4',
    })
    if item_index:
        ydl_opts['noplaylist'] = False
        ydl_opts['playlist_items'] = str(item_index)
    if audio_only:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_data = ydl.extract_info(url, download=True)
            target = info_data
            if isinstance(info_data, dict) and info_data.get('entries'):
                entries_list = list(info_data['entries'])
                if entries_list:
                    target = entries_list[0]
            filepath = ydl.prepare_filename(target)
            if audio_only:
                filepath = os.path.splitext(filepath)[0] + '.mp3'

        if not os.path.exists(filepath):
            raise FileNotFoundError('El archivo no se generó correctamente.')

        title = target.get('title', 'video')
        safe_title = ''.join(c for c in title if c.isalnum() or c in ' -_').strip()[:60] or 'video'
        ext = 'mp3' if audio_only else os.path.splitext(filepath)[1].lstrip('.') or 'mp4'
        download_name = f'{safe_title}.{ext}'

        def generate():
            try:
                with open(filepath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        mimetype = 'audio/mpeg' if audio_only else 'video/mp4'
        return Response(
            stream_with_context(generate()),
            mimetype=mimetype,
            headers={'Content-Disposition': f'attachment; filename="{download_name}"'}
        )
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({'error': f'Falló la descarga: {str(e)}'}), 502


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
