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
import signal
from collections import deque

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://ssh101.bozztv.com:1935/ssh101"
STREAM_KEY = os.getenv("STREAM_KEY") or "playtv"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

M3U_URL = os.getenv("M3U_URL") or "https://raw.githubusercontent.com/ino8090/0101/refs/heads/main/deneme.m3u"
LOGO_URL = os.getenv("LOGO_URL") or "https://raw.githubusercontent.com/ino8090/0101/refs/heads/main/playtv.png"

STATE_FILE_NAME = os.getenv("STATE_FILE_NAME", "yerli.json")
GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY")

STREAM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
STREAM_REFERER = "https://vidmody.com/"

LOGO_OPACITY = float(os.getenv("LOGO_OPACITY", "1.0"))
TEXT_OPACITY = float(os.getenv("TEXT_OPACITY", "1.0"))
BOLD_FONT_PATH = os.getenv("BOLD_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

TICKER_TEXT = ""

STATE_SAVE_INTERVAL_SECONDS = int(os.getenv("STATE_SAVE_INTERVAL_SECONDS", "10"))
LINK_CHANGE_REWIND_SECONDS = int(os.getenv("LINK_CHANGE_REWIND_SECONDS", "0"))
MIN_VALID_MOVIE_DURATION_SECONDS = int(os.getenv("MIN_VALID_MOVIE_DURATION_SECONDS", "180"))


def format_hms(total_seconds):
    total_seconds = int(total_seconds)
    hrs = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


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
_live_state = {"index": 0, "seconds": 0, "url": "", "title": "", "proc": None}


def get_local_state():
    if os.path.exists(STATE_FILE_NAME):
        if os.path.getsize(STATE_FILE_NAME) == 0:
            return 0, 0, "", ""
        try:
            with open(STATE_FILE_NAME, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_index", 0), data.get("last_seconds", 0), data.get("last_url", ""), data.get("last_title", "")
        except Exception as e:
            print(f"⚠️ Yerel state okuma hatası: {e}")
    return 0, 0, "", ""


def update_local_state(index, seconds, url="", title=""):
    tmp_file = f"{STATE_FILE_NAME}.tmp"
    try:
        data = {"last_index": int(index), "last_seconds": int(seconds), "last_url": url, "last_title": title}
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, STATE_FILE_NAME)
        print(f"💾 Konum yerel dosyaya kaydedildi => İndeks: {index}, Saniye: {int(seconds)}")
    except Exception as e:
        print(f"⚠️ Yerel state yazma hatası: {e}")


def _save_and_exit(signum=None, frame=None):
    try:
        if _live_state["url"]:
            update_local_state(_live_state["index"], _live_state["seconds"], _live_state["url"], _live_state["title"])
    except Exception:
        pass
    try:
        p = _live_state.get("proc")
        if p is not None and p.poll() is None:
            p.kill()
    except Exception:
        pass
    sys.exit(0)


def get_m3u_playlist(m3u_url):
    try:
        headers = {'User-Agent': STREAM_USER_AGENT, 'Referer': STREAM_REFERER}
        response = requests.get(m3u_url, headers=headers, timeout=15)
        if response.status_code == 200:
            content = response.text.strip()
            if "#EXTM3U" in content or "#EXTINF" in content:
                lines = content.splitlines()
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
                if playlist:
                    return playlist
            
            return [{"url": m3u_url, "title": os.path.basename(m3u_url.split('?')[0])}]
    except Exception as e:
        print(f"⚠️ İçerik analiz ve çekme hatası: {e}")
    return [{"url": m3u_url, "title": os.path.basename(m3u_url.split('?')[0])}]


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


def get_stream_duration(url, headers_arg):
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-headers', headers_arg,
            '-allowed_extensions', 'ALL',  # FFprobe için de uzantı kısıtlamasını kaldır
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            url
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, text=True)
        if res.returncode == 0:
            val = res.stdout.strip()
            if val and val != "N/A":
                return float(val)
    except Exception as e:
        print(f"⚠️ ffprobe süre sorgulama uyarısı: {e}")
    return None


def start_m3u_stream():
    print(f"🔧 Kullanılan Kaynak: {M3U_URL}")
    print(f"🔧 Kullanılan Logo 1: {LOGO_URL}")
    print(f"🔧 State dosyası    : {STATE_FILE_NAME}")
    print(f"🔧 RTMP hedefi      : {RTMP_SERVER}")

    download_logo()
    current_index, last_seconds, last_url, last_title = get_local_state()

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
            last_title = ""

        current_item = playlist[current_index]
        target_stream_url = current_item["url"]
        film_title = current_item["title"]

        if last_seconds > 0 and last_url and target_stream_url != last_url:
            if last_title and film_title != last_title:
                print(f"🔄 Bu sıradaki ({current_index + 1}) içerik değişmiş ({last_title} -> {film_title}), video baştan başlatılacak.")
                last_seconds = 0
            else:
                old_seconds = last_seconds
                last_seconds = max(0, last_seconds - LINK_CHANGE_REWIND_SECONDS)
                print(f"🔄 İçerik linki yenilenmiş, aynı film. {old_seconds}s yerine {last_seconds}s'den devam edilecek.")

        last_url = target_stream_url
        last_title = film_title

        write_title_file(film_title)
        _live_state.update({"index": current_index, "seconds": last_seconds, "url": target_stream_url, "title": film_title})

        headers_arg = (
            f"User-Agent: {STREAM_USER_AGENT}\r\n"
            f"Referer: https://vidmody.com/\r\n"
            f"Origin: https://vidmody.com\r\n"
        )

        print(f"🔍 Kaynak video içeriği ffprobe ile doğrulanıyor...")
        media_duration = get_stream_duration(target_stream_url, headers_arg)
        if media_duration:
            print(f"⏱️ Filmin Toplam Süresi: {format_hms(media_duration)} ({media_duration:.1f}s)")
            if last_seconds >= media_duration - 15:
                print(f"✅ Bu içerik zaten bitmiş görünüyor (Kayıtlı: {last_seconds}s >= Süre: {media_duration:.1f}s). Sıradakine geçiliyor.")
                current_index += 1
                last_seconds = 0
                last_url = ""
                last_title = ""
                update_local_state(current_index, 0, "", "")
                continue
        else:
            print(f"⚠️ Filmin toplam süresi ffprobe ile alınamadı, içerik akışı doğrudan deneniyor.")

        if media_duration and last_seconds > media_duration:
            last_seconds = 0

        print("=" * 60)
        print("📺 Maxanimasyon Canlı Aktarım Yayını (1080p 25fps - 2500k) Başlatılıyor")
        print(f"🎬 Oynatılan İçerik  : {film_title}")
        print(f"⏱️ Başlangıç Saniyesi: {last_seconds}")
        print(f"🚀 Hedef RTMP       : {RTMP_SERVER}")

        input_options = [
            '-headers', headers_arg,
            '-allowed_extensions', 'ALL',  # <- JPG gibi farklı segment uzantılarına izin verir
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

            input_args = (
                input_options + ['-re', '-i', video_url] +
                input_options + ['-re', '-i', audio_url]
            )
            audio_map = ['-map', '1:a:0?']
            logo1_input_index = 2
        else:
            input_args = input_options + ['-re', '-i', target_stream_url]
            audio_map = ['-map', '0:a:0?']
            logo1_input_index = 1

        print("=" * 60)

        print_dashboard(film_title, current_index, len(playlist), last_seconds, status="🟡 Başlatılıyor")
        write_step_summary(film_title, current_index, len(playlist), last_seconds, status="🟡 Başlatılıyor")

        has_logo1 = os.path.exists('logo.png') and os.path.getsize('logo.png') > 0

        title_drawtext = (
            f"drawtext=textfile='title.txt':reload=1:fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white@{TEXT_OPACITY}:fontsize=23:"
            f"x=50:y=main_h-th-50"
        )

        ticker_drawtext = (
            f"drawtext=text='{TICKER_TEXT}':fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white:fontsize=0:"
            f"box=1:boxcolor=black@0.0:boxborderw=0:"
            f"x='w-mod(t*0\\, w+tw)':y=h-th-0"
        )

        if has_logo1:
            logo_inputs = ['-i', 'logo.png']
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=25[main];'
                f'[{logo1_input_index}:v]scale=-2:78,format=rgba,'
                f'colorchannelmixer=aa={LOGO_OPACITY}[logo1];'
                '[main][logo1]overlay=main_w-overlay_w-60:55[tmp1];'
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

        print("▶ FFmpeg başlatıldı, 1080p 25fps @ 2500k içerik aktarılıyor...")

        process = subprocess.Popen(
            command,
            stdout=publisher.proc.stdin,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        _live_state["proc"] = process

        base_seconds = last_seconds
        last_save_time = time.time()
        last_dashboard_time = time.time()
        current_stream_seconds = base_seconds
        stderr_tail = deque(maxlen=40)
        played_seconds = 0.0

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
                    current_stream_seconds = base_seconds + played_seconds
                    _live_state["seconds"] = current_stream_seconds

                    now = time.time()

                    if now - last_save_time > STATE_SAVE_INTERVAL_SECONDS:
                        update_local_state(current_index, current_stream_seconds, target_stream_url, film_title)
                        last_save_time = now

                    if now - last_dashboard_time > 30:
                        print_dashboard(film_title, current_index, len(playlist), current_stream_seconds)
                        write_step_summary(film_title, current_index, len(playlist), current_stream_seconds)
                        last_dashboard_time = now

        is_actually_finished = False
        if media_duration and current_stream_seconds >= media_duration - 15:
            is_actually_finished = True

        if (process.returncode == 0 and played_seconds >= MIN_VALID_MOVIE_DURATION_SECONDS) or is_actually_finished:
            print(f"✅ İçerik bitti, sıradakine geçiliyor.")
            write_step_summary(film_title, current_index, len(playlist), current_stream_seconds, status="✅ Bitti, sıradakine geçiliyor")
            current_index += 1
            last_seconds = 0
            last_url = ""
            last_title = ""
            update_local_state(current_index, 0, "", "")
            consecutive_fast_failures = 0
        else:
            print(f"⚠️ Yayın koptu veya erken kapandı (Oynatılan: {format_hms(played_seconds)}).")
            if stderr_tail:
                print("🧾 FFmpeg son log satırları:")
                for tail_line in stderr_tail:
                    print(f"   {tail_line}")

            write_step_summary(film_title, current_index, len(playlist), current_stream_seconds, status="🔴 Bağlantı koptu, tekrar denenecek")
            
            last_seconds = current_stream_seconds
            duration_this_attempt = current_stream_seconds - base_seconds
            if duration_this_attempt < FAST_FAIL_THRESHOLD_SECONDS:
                consecutive_fast_failures += 1
            else:
                consecutive_fast_failures = 0

            last_url = target_stream_url
            last_title = film_title
            update_local_state(current_index, last_seconds, last_url, film_title)

        if consecutive_fast_failures > 0:
            retry_delay = min(5 * (2 ** consecutive_fast_failures), MAX_RETRY_DELAY_SECONDS)
        else:
            retry_delay = 5

        print(f"⚠️ {retry_delay} saniye sonra tekrar bağlanılıyor...")
        time.sleep(retry_delay)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _save_and_exit)
    signal.signal(signal.SIGINT, _save_and_exit)
    try:
        start_m3u_stream()
    except KeyboardInterrupt:
        print("\n🛑 Durduruluyor...")
    finally:
        publisher.stop()
