import os
import uuid
import shutil
import tempfile

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)  # Permite que tu página de GitHub Pages llame a este servidor

COMMON_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
}


@app.route('/')
def home():
    return jsonify({'status': 'ok', 'message': 'Slim Videos backend corriendo'})


@app.route('/api/info')
def info():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Falta el parámetro url'}), 400

    try:
        with yt_dlp.YoutubeDL(COMMON_OPTS) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({'error': f'No se pudo procesar el video: {str(e)}'}), 502

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
        fmt = 'bestvideo+bestaudio/best'

    ydl_opts = dict(COMMON_OPTS)
    ydl_opts.update({
        'outtmpl': out_template,
        'format': fmt,
        'merge_output_format': 'mp4',
    })
    if audio_only:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_data = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info_data)
            if audio_only:
                filepath = os.path.splitext(filepath)[0] + '.mp3'

        if not os.path.exists(filepath):
            raise FileNotFoundError('El archivo no se generó correctamente.')

        title = info_data.get('title', 'video')
        safe_title = ''.join(c for c in title if c.isalnum() or c in ' -_').strip()[:60] or 'video'
        ext = 'mp3' if audio_only else 'mp4'
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
