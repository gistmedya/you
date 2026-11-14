#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time
import os
import re
import json
import requests
import threading
import hashlib
import http.server
import socketserver
import urllib.parse
from collections import deque

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://rtmp.zemmedya.com/zem"
STREAM_KEY = os.getenv("STREAM_KEY") or "Maxmedya?key=Zemmedya-3592302306"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

M3U_URL = os.getenv("M3U_URL") or "https://raw.githubusercontent.com/gistmedya/0101/refs/heads/main/deneme.m3u"
LOGO_URL = os.getenv("LOGO_URL") or "https://raw.githubusercontent.com/gistmedya/0101/refs/heads/main/file_000000007be48210a068edefa7260629.png"

STATE_FILE_NAME = os.getenv("STATE_FILE_NAME", "fixtv.json")
GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY")

STREAM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
STREAM_REFERER = "https://vidmody.com/"

# Logo ve yazı opaklık ayarları (0.0 - 1.0 arası)
LOGO_OPACITY = float(os.getenv("LOGO_OPACITY", "0.4"))
TEXT_OPACITY = float(os.getenv("TEXT_OPACITY", "0.5"))
BOLD_FONT_PATH = os.getenv("BOLD_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# Ekranın altındaki kayan yazı metni
TICKER_TEXT = ""


def format_hms(total_seconds):
    """Saniyeyi SS:DD:SS formatına çevirir."""
    total_seconds = int(total_seconds)
    hrs = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


# ===================== HLS SEGMENT PROXY =====================
class HlsProxy:
    """
    HLS manifest'lerindeki 'gizlenmiş' segment uzantılarını (ör. .jpg) FFmpeg'in
    kabul edeceği tiplere dönüştürmek için yerel bir HTTP proxy.
    """

    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
        self.upstream_headers = {}
        self.segment_cache = {}
        self.lock = threading.Lock()

    def start(self):
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                try:
                    proxy._handle(self)
                except Exception:
                    try:
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    except Exception:
                        pass

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = ThreadedServer((self.host, self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"🌐 HLS proxy çalışıyor => http://{self.host}:{self.port}")
        return self

    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def register_segment(self, upstream_url):
        path = urllib.parse.urlparse(upstream_url).path
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "ts"
        allowed = {"ts", "m4s", "mp4", "aac", "m4a", "m4v", "mp3", "ac3", "eac3", "vtt", "webvtt"}
        served_ext = ext if ext in allowed else "ts"

        token = hashlib.sha1(upstream_url.encode("utf-8")).hexdigest()[:16]
        with self.lock:
            self.segment_cache[token] = (upstream_url, served_ext)
        return f"{self.base_url()}/seg/{token}.{served_ext}"

    def _handle(self, handler):
        parsed = urllib.parse.urlparse(handler.path)
        path = parsed.path

        if path.startswith("/seg/"):
            token = path[len("/seg/"):].split(".")[0]
            with self.lock:
                entry = self.segment_cache.get(token)
            if not entry:
                handler.send_response(404)
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return

            upstream_url, served_ext = entry
            ctype = "video/mp2t"
            if served_ext in ("m4s", "mp4", "m4v"):
                ctype = "video/iso.segment"
            elif served_ext in ("aac", "m4a", "mp3", "ac3", "eac3"):
                ctype = "audio/mp4"
            elif served_ext in ("vtt", "webvtt"):
                ctype = "text/vtt"

            try:
                r = requests.get(
                    upstream_url,
                    headers=self.upstream_headers,
                    stream=True,
                    timeout=30,
                )
            except Exception:
                handler.send_response(502)
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return

            handler.send_response(r.status_code)
            handler.send_header("Content-Type", ctype)
            length = r.headers.get("Content-Length")
            if length:
                handler.send_header("Content-Length", length)
            else:
                handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()

            try:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        handler.wfile.write(chunk)
            except Exception:
                pass
            return

        handler.send_response(404)
        handler.send_header("Content-Length", "0")
        handler.end_headers()


hls_proxy = HlsProxy()
_hls_proxy_started = False


def _ensure_proxy_started():
    global _hls_proxy_started
    if not _hls_proxy_started:
        hls_proxy.start()
        _hls_proxy_started = True


def _rewrite_m3u8(base_url, manifest_text):
    """
    Manifest içindeki tüm segment / key / init URL'lerini proxy'ye yönlendirir.
    """
    lines = manifest_text.splitlines()
    out_lines = []

    for raw in lines:
        line = raw.strip()
        if not line:
            out_lines.append(raw)
            continue

        if line.startswith("#"):
            def repl(match):
                uri = match.group(1)
                absolute = urllib.parse.urljoin(base_url, uri)
                return f'URI="{absolute}"'
            new_line = re.sub(r'URI="([^"]+)"', repl, line)
            out_lines.append(new_line)
            continue

        absolute = urllib.parse.urljoin(base_url, line)
        local = hls_proxy.register_segment(absolute)
        out_lines.append(local)

    return "\n".join(out_lines) + "\n"


def resolve_stream_url(url):
    """
    Uzantısız da olsa içeriği HLS manifest ise (#EXTM3U içeriyorsa),
    segment URL'lerini yerel proxy'ye yönlendirir ve yerel manifest yolunu döndürür.
    Değilse orijinal URL'yi döndürür.
    """
    _ensure_proxy_started()
    hls_proxy.upstream_headers = {
        "User-Agent": STREAM_USER_AGENT,
        "Referer": STREAM_REFERER,
        "Origin": "https://vidmody.com",
    }

    try:
        r = requests.get(url, headers=hls_proxy.upstream_headers, timeout=20)
        if r.status_code != 200:
            print(f"⚠️ Kaynak indirilemedi (status={r.status_code}), orijinal URL kullanılacak.")
            return url

        # İçerik HLS manifest mi? (uzantıdan bağımsız)
        if "#EXTM3U" not in r.text:
            ctype = r.headers.get('Content-Type', '?')
            print(f"ℹ️ Kaynak HLS manifest değil (Content-Type: {ctype}), orijinal URL kullanılacak.")
            return url

        manifest_text = r.text
        base_url = url
        print(f"✅ HLS manifest algılandı: {url[:100]}")

        # Master playlist ise ilk variant'ı seç
        if "#EXT-X-STREAM-INF" in manifest_text:
            variant = None
            lines = manifest_text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    for j in range(i + 1, len(lines)):
                        nxt = lines[j].strip()
                        if nxt and not nxt.startswith("#"):
                            variant = urllib.parse.urljoin(base_url, nxt)
                            break
                    if variant:
                        break
            if not variant:
                print("⚠️ Master manifest'te variant bulunamadı, orijinal URL kullanılacak.")
                return url
            print(f"🔀 Master manifest algılandı, variant seçildi: {variant[:100]}")
            r2 = requests.get(variant, headers=hls_proxy.upstream_headers, timeout=20)
            if r2.status_code != 200 or "#EXTM3U" not in r2.text:
                print(f"⚠️ Variant manifest alınamadı (status={r2.status_code}).")
                return url
            manifest_text = r2.text
            base_url = variant

        rewritten = _rewrite_m3u8(base_url, manifest_text)

        manifest_path = os.path.abspath("proxied_manifest.m3u8")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(rewritten)

        # Debug: ilk birkaç satırı logla
        debug_lines = rewritten.splitlines()
        print(f"🔧 Manifest proxy'ye yönlendirildi => {manifest_path}")
        print("   📄 Manifest ilk satırlar:")
        for dl in debug_lines[:8]:
            print(f"      {dl[:140]}")

        return manifest_path
    except Exception as e:
        print(f"⚠️ Manifest rewrite hatası: {e}, orijinal URL kullanılacak.")
        return url


# ===================== KALICI RTMP YAYINCISI =====================
class RtmpPublisher:
    def __init__(self):
        self.proc = None
        self.started_at = 0.0
        self.tail = deque(maxlen=30)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure_running(self):
        if self.alive():
            return
        self.stop()
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-fflags', '+genpts',
            '-analyzeduration', '3000000',
            '-probesize', '3000000',
            '-f', 'mpegts', '-i', 'pipe:0',
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            '-flvflags', 'no_duration_filesize',
            '-f', 'flv',
            RTMP_SERVER
        ]
        print(f"📡 Kalıcı RTMP yayıncısı başlatılıyor => {RTMP_SERVER}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        self.started_at = time.time()
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

    def _drain(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b''):
                self.tail.append(raw.decode('utf-8', 'replace').rstrip())
        except Exception:
            pass

    def clock(self):
        return time.time() - self.started_at

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        self.proc = None


publisher = RtmpPublisher()


def get_local_state():
    if os.path.exists(STATE_FILE_NAME):
        if os.path.getsize(STATE_FILE_NAME) == 0:
            print(f"⚠️ Yerel state dosyası boş ({STATE_FILE_NAME}), 0'dan başlanıyor.")
            return 0, 0, ""
        try:
            with open(STATE_FILE_NAME, "r", encoding="utf-8") as f:
                data = json.load(f)
                idx = data.get("last_index", 0)
                sec = data.get("last_seconds", 0)
                url = data.get("last_url", "")
                print(f"✅ Yerel state okundu ({STATE_FILE_NAME}) => İndeks: {idx}, Saniye: {sec}")
                return idx, sec, url
        except Exception as e:
            print(f"⚠️ Yerel state okuma hatası: {e}")
    else:
        print(f"ℹ️ Yerel state dosyası bulunamadı, 0'dan başlanıyor.")
    return 0, 0, ""


def update_local_state(index, seconds, url=""):
    try:
        data = {"last_index": int(index), "last_seconds": int(seconds), "last_url": url}
        with open(STATE_FILE_NAME, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"💾 Konum yerel dosyaya kaydedildi => İndeks: {index}, Saniye: {int(seconds)}")
    except Exception as e:
        print(f"⚠️ Yerel state yazma hatası: {e}")


def get_m3u_playlist(m3u_url):
    try:
        headers = {
            'User-Agent': STREAM_USER_AGENT,
            'Referer': STREAM_REFERER
        }
        response = requests.get(m3u_url, headers=headers, timeout=15)
        if response.status_code == 200:
            lines = response.text.splitlines()
            playlist = []
            pending_title = None
            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith('#EXTINF'):
                    match = re.search(r',(.+)$', line)
                    pending_title = match.group(1).strip() if match else None
                elif not line.startswith('#') and line.startswith('http'):
                    title = pending_title or os.path.basename(line.split('?')[0])
                    playlist.append({"url": line, "title": title})
                    pending_title = None
            return playlist
    except Exception as e:
        print(f"⚠️ M3U çekme hatası: {e}")
    return [{"url": m3u_url, "title": os.path.basename(m3u_url)}]


def download_logo():
    headers = {'User-Agent': STREAM_USER_AGENT}
    try:
        response = requests.get(LOGO_URL, headers=headers, timeout=15)
        if response.status_code == 200 and len(response.content) > 0:
            with open('logo.png', 'wb') as f:
                f.write(response.content)
            print("✅ 1. Logo başarıyla indirildi.")
    except Exception as e:
        print(f"⚠️ 1. Logo indirme hatası: {e}")


def write_title_file(title):
    try:
        with open('title.txt', 'w', encoding='utf-8') as f:
            f.write(title)
    except Exception as e:
        print(f"⚠️ Başlık dosyası yazma hatası: {e}")


def print_dashboard(title, index, playlist_len, seconds, status="🟢 Yayında"):
    print("┌" + "─" * 58 + "┐")
    print(f"│ 🎬 İçerik         : {title[:36]:<36} │")
    print(f"│ 🔢 Sıra           : {index + 1}/{playlist_len:<32} │")
    print(f"│ ⏱️  Geçen Süre     : {format_hms(seconds):<36} │")
    print(f"│ 📡 Durum          : {status:<36} │")
    print("└" + "─" * 58 + "┘")


def write_step_summary(title, index, playlist_len, seconds, status="🟢 Yayında"):
    if not GITHUB_STEP_SUMMARY:
        return
    try:
        content = (
            "## 📺 Canlı Yayın Durumu (Maxanimasyon)\n\n"
            "| Alan | Değer |\n"
            "|---|---|\n"
            f"| 🎬 Şu an oynayan içerik | {title} |\n"
            f"| 🔢 Playlist sırası | {index + 1} / {playlist_len} |\n"
            f"| ⏱️ Geçen süre | {format_hms(seconds)} (sa:dk:sn) |\n"
            f"| 📡 Durum | {status} |\n"
            f"| 🕒 Son güncelleme | {time.strftime('%Y-%m-%d %H:%M:%S')} |\n"
        )
        with open(GITHUB_STEP_SUMMARY, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"⚠️ Step summary yazma hatası: {e}")


def start_m3u_stream():
    print(f"🔧 Kullanılan M3U   : {M3U_URL}")
    print(f"🔧 Kullanılan Logo 1: {LOGO_URL}")
    print(f"🔧 State dosyası    : {STATE_FILE_NAME}")
    print(f"🔧 RTMP hedefi      : {RTMP_SERVER}")

    download_logo()

    current_index, last_seconds, last_url = get_local_state()

    consecutive_fast_failures = 0
    FAST_FAIL_THRESHOLD_SECONDS = 20
    MAX_RETRY_DELAY_SECONDS = 120

    while True:
        playlist = get_m3u_playlist(M3U_URL)
        if not playlist:
            time.sleep(10)
            continue

        if current_index >= len(playlist):
            current_index = 0
            last_seconds = 0
            last_url = ""

        current_item = playlist[current_index]
        target_stream_url = current_item["url"]
        film_title = current_item["title"]

        if last_seconds > 0 and last_url and target_stream_url != last_url:
            print(f"🔄 Bu sıradaki ({current_index + 1}) içeriğin linki değişmiş, video baştan başlatılacak.")
            print(f"   Eski link: {last_url}")
            print(f"   Yeni link: {target_stream_url}")
            last_seconds = 0

        last_url = target_stream_url

        write_title_file(film_title)

        print("=" * 60)
        print("📺 Maxanimasyon Canlı Aktarım Yayını (1080p 25fps - 2500k) Başlatılıyor")
        print(f"🎬 Oynatılan İçerik  : {film_title}")
        print(f"⏱️ Başlangıç Saniyesi: {last_seconds}")
        print(f"🚀 Hedef RTMP       : {RTMP_SERVER}")

        headers_arg = (
            f"User-Agent: {STREAM_USER_AGENT}\r\n"
            f"Referer: https://vidmody.com/\r\n"
            f"Origin: https://vidmody.com\r\n"
        )

        ss_arg = ['-ss', str(last_seconds)] if last_seconds > 0 else []

        input_options = [
            '-headers', headers_arg,
            '-protocol_whitelist', 'file,http,https,tcp,tls,crypto',
            '-err_detect', 'ignore_err',
            '-analyzeduration', '10000000',
            '-probesize', '10000000',
            '-reconnect', '1',
            '-reconnect_at_eof', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '10',
            '-rw_timeout', '10000000'
        ]

        if ";" in target_stream_url:
            video_url, audio_url = target_stream_url.split(";", 1)
            video_url = video_url.strip()
            audio_url = audio_url.strip()

            ffmpeg_video_url = resolve_stream_url(video_url)
            ffmpeg_audio_url = resolve_stream_url(audio_url)

            print(f"🎥 Video Bağlantısı : {video_url}")
            print(f"🔊 Ses Bağlantısı   : {audio_url}")

            input_args = (
                input_options + ss_arg + ['-re', '-i', ffmpeg_video_url] +
                input_options + ss_arg + ['-re', '-i', ffmpeg_audio_url]
            )
            audio_map = ['-map', '1:a:0?']
            logo1_input_index = 2
        else:
            print(f"📡 Kaynak Yayın     : {target_stream_url}")
            ffmpeg_input_url = resolve_stream_url(target_stream_url)
            input_args = input_options + ss_arg + ['-re', '-i', ffmpeg_input_url]
            audio_map = ['-map', '0:a:0?']
            logo1_input_index = 1

        print("=" * 60)

        print_dashboard(film_title, current_index, len(playlist), last_seconds, status="🟡 Başlatılıyor")
        write_step_summary(film_title, current_index, len(playlist), last_seconds, status="🟡 Başlatılıyor")

        has_logo1 = os.path.exists('logo.png') and os.path.getsize('logo.png') > 0

        title_drawtext = (
            f"drawtext=textfile='title.txt':reload=1:fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white@{TEXT_OPACITY}:fontsize=30:"
            f"x=80:y=main_h-th-80"
        )

        ticker_drawtext = (
            f"drawtext=text='{TICKER_TEXT}':fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white:fontsize=0:"
            f"box=1:boxcolor=black@0.0:boxborderw=10:"
            f"x='w-mod(t*0\, w+tw)':y=h-th-20"
        )

        if has_logo1:
            logo_inputs = ['-i', 'logo.png']
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=25[main];'
                f'[{logo1_input_index}:v]scale=-2:91,format=rgba,'
                f'colorchannelmixer=aa={LOGO_OPACITY}[logo1];'
                '[main][logo1]overlay=main_w-overlay_w-104:80[tmp1];'
                f'[tmp1]{title_drawtext}[tmp2];'
                f'[tmp2]{ticker_drawtext}[v]'
            )
        else:
            logo_inputs = []
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=25[main];'
                f'[main]{title_drawtext}[tmp1];'
                f'[tmp1]{ticker_drawtext}[v]'
            )

        publisher.ensure_running()

        ts_offset = publisher.clock() + 1.0

        command = [
            'ffmpeg'
        ] + input_args + logo_inputs + [
            '-filter_complex', filter_str,
            '-map', '[v]'
        ] + audio_map + [
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-pix_fmt', 'yuv420p',
            '-r', '25',
            '-b:v', '2500k',
            '-maxrate', '2500k',
            '-bufsize', '3000k',
            '-g', '50',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ac', '2',
            '-ar', '44100',
            '-output_ts_offset', f'{ts_offset:.3f}',
            '-flush_packets', '1',
            '-f', 'mpegts',
            'pipe:1'
        ]

        print("▶ FFmpeg başlatıldı, 1080p 25fps @ 2500k yayın iletiliyor...")

        process = subprocess.Popen(
            command,
            stdout=publisher.proc.stdin,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        last_save_time = time.time()
        last_dashboard_time = time.time()
        current_stream_seconds = last_seconds
        stderr_tail = deque(maxlen=40)

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break

            if line:
                stderr_tail.append(line.rstrip())

            if "time=" in line:
                time_match = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
                if time_match:
                    hrs, mins, secs = time_match.groups()
                    played_seconds = int(hrs) * 3600 + int(mins) * 60 + float(secs)
                    current_stream_seconds = last_seconds + played_seconds

                    now = time.time()

                    if now - last_save_time > 30:
                        update_local_state(current_index, current_stream_seconds, target_stream_url)
                        last_save_time = now

                    if now - last_dashboard_time > 30:
                        print_dashboard(film_title, current_index, len(playlist), current_stream_seconds)
                        write_step_summary(film_title, current_index, len(playlist), current_stream_seconds)
                        last_dashboard_time = now

        if process.returncode == 0:
            print("✅ İçerik bitti, sıradakine geçiliyor.")
            write_step_summary(film_title, current_index, len(playlist), current_stream_seconds, status="✅ Bitti, sıradakine geçiliyor")
            current_index += 1
            last_seconds = 0
            last_url = ""
            update_local_state(current_index, 0, "")
            consecutive_fast_failures = 0
        else:
            print(f"⚠️ Yayın koptu (Return Code: {process.returncode}). Aynı saniyeden tekrar denenecek.")
            if stderr_tail:
                print("🧾 FFmpeg son log satırları:")
                for tail_line in stderr_tail:
                    print(f"   {tail_line}")
            write_step_summary(film_title, current_index, len(playlist), current_stream_seconds, status="🔴 Bağlantı koptu, tekrar denenecek")
            duration_this_attempt = current_stream_seconds - last_seconds
            if duration_this_attempt < FAST_FAIL_THRESHOLD_SECONDS:
                consecutive_fast_failures += 1
            else:
                consecutive_fast_failures = 0
            last_seconds = current_stream_seconds
            last_url = target_stream_url
            update_local_state(current_index, last_seconds, last_url)

        if consecutive_fast_failures > 0:
            retry_delay = min(5 * (2 ** consecutive_fast_failures), MAX_RETRY_DELAY_SECONDS)
        else:
            retry_delay = 5

        print(f"⚠️ {retry_delay} saniye sonra tekrar bağlanılıyor...")
        time.sleep(retry_delay)


if __name__ == "__main__":
    try:
        start_m3u_stream()
    except KeyboardInterrupt:
        print("\n🛑 Durduruluyor...")
    finally:
        publisher.stop()
