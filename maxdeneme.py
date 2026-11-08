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
from collections import deque

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://ssh101.bozztv.com:1935/ssh101"
STREAM_KEY = os.getenv("STREAM_KEY") or "playtv"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

M3U_URL = os.getenv("M3U_URL") or "https://raw.githubusercontent.com/ino8090/0101/refs/heads/main/deneme.m3u"
LOGO_URL = os.getenv("LOGO_URL") or "https://raw.githubusercontent.com/ino8090/0101/refs/heads/main/1787745128505.png"

STATE_FILE_NAME = os.getenv("STATE_FILE_NAME", "fixtv.json")
GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY")

STREAM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
STREAM_REFERER = "https://ha.vixolity.com/"

# Logo ve yazı opaklık ayarları (0.0 - 1.0 arası)
LOGO_OPACITY = float(os.getenv("LOGO_OPACITY", "1.0"))
TEXT_OPACITY = float(os.getenv("TEXT_OPACITY", "1.0"))
BOLD_FONT_PATH = os.getenv("BOLD_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# Decoder'ı tek thread'e zorlamak için
DECODER_THREADS = os.getenv("DECODER_THREADS", "1")

# Watchdog Zaman Aşımları (Saniye)
WATCHDOG_STARTUP_SECONDS = int(os.getenv("WATCHDOG_STARTUP_SECONDS", "60"))  # İlk açılış toleransı
WATCHDOG_TIMEOUT_SECONDS = int(os.getenv("WATCHDOG_TIMEOUT_SECONDS", "45"))  # Yayın ortası donma toleransı

# Tolerans ve Geri Sarma Ayarları
END_TOLERANCE_SECONDS = 15  # Bitişe bu kadar saniye kaldıysa film gerçekten bitmiş sayılır
SEEK_BACKOFF_SECONDS = 10   # Sürekli çöken noktada kaç saniye geriye sarılacak
LINK_CHANGE_REWIND_SECONDS = int(os.getenv("LINK_CHANGE_REWIND_SECONDS", "15"))


def format_hms(total_seconds):
    """Saniyeyi SS:DD:SS formatına çevirir."""
    total_seconds = max(0, int(total_seconds))
    hrs = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


# ===================== KALICI RTMP YAYINCISI =====================
class RtmpPublisher:
    """
    RTMP sunucusuna SÜREKLİ bağlı kalan tek bir FFmpeg süreci.
    Filmleri kodlayan FFmpeg süreçleri çıktılarını (MPEG-TS) bu sürecin stdin'ine yazar.
    """

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
_duration_cache = {}


def probe_url_of(stream_url):
    return stream_url.split(";")[0].strip() if ";" in stream_url else stream_url


def prefetch_duration(stream_url):
    url = probe_url_of(stream_url)
    duration = get_video_duration_ffprobe(url)
    if duration > 0:
        _duration_cache[url] = duration


def get_video_duration_ffprobe(video_url):
    """FFprobe ile videonun GERÇEK toplam süresini çeker."""
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        '-headers', f"User-Agent: {STREAM_USER_AGENT}\r\nReferer: {STREAM_REFERER}\r\n",
        video_url
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=12).decode('utf-8', 'replace').strip()
        duration = float(output.splitlines()[-1].strip())
        if duration > 0:
            print(f"⏱️ ffprobe ile toplam süre tespit edildi: {duration:.1f} saniye ({format_hms(duration)})")
            return duration
    except Exception as e:
        print(f"⚠️ ffprobe ile süre okunamadı: {e}")
    return 0.0


def is_really_finished(current_sec, total_sec):
    """
    Sadece FFmpeg'in 0 vermesine güvenmez.
    İzlenen sürenin toplam süreye ulaşıp ulaşmadığını kontrol eder (Sahte Bitiş Koruması).
    """
    if total_sec <= 0:
        return True  # Toplam süre bilinmiyorsa FFmpeg'in 0 çıkışına güvenmek zorundayız.
    return (total_sec - current_sec) <= END_TOLERANCE_SECONDS


def get_local_state():
    """Yerel state dosyasından son durumu okur."""
    if os.path.exists(STATE_FILE_NAME):
        if os.path.getsize(STATE_FILE_NAME) == 0:
            return 0, 0, "", ""
        try:
            with open(STATE_FILE_NAME, "r", encoding="utf-8") as f:
                data = json.load(f)
                return (
                    data.get("last_index", 0),
                    data.get("last_seconds", 0),
                    data.get("last_url", ""),
                    data.get("last_title", "")
                )
        except Exception as e:
            print(f"⚠️ Yerel state okuma hatası: {e}")
    return 0, 0, "", ""


def update_local_state(index, seconds, url="", title=""):
    """Atomik yazım mantığıyla son konumu kaydeder."""
    tmp_file = f"{STATE_FILE_NAME}.tmp"
    try:
        data = {
            "last_index": int(index),
            "last_seconds": int(seconds),
            "last_url": url,
            "last_title": title,
        }
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, STATE_FILE_NAME)
        print(f"💾 Konum kaydedildi => İndeks: {index}, Saniye: {int(seconds)}")
    except Exception as e:
        print(f"⚠️ Yerel state yazma hatası: {e}")


def get_m3u_playlist(m3u_url):
    try:
        headers = {'User-Agent': STREAM_USER_AGENT, 'Referer': STREAM_REFERER}
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
            print("✅ Logo başarıyla indirildi.")
    except Exception as e:
        print(f"⚠️ Logo indirme hatası: {e}")


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
    print(f"🔧 Kullanılan Logo  : {LOGO_URL}")
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

        # Link Değişikliği / Süre Rewind Kontrolü
        if last_seconds > 0 and last_url and target_stream_url != last_url:
            if last_title and film_title == last_title:
                old_seconds = last_seconds
                last_seconds = max(0, last_seconds - LINK_CHANGE_REWIND_SECONDS)
                print(f"🔄 İçerik linki yenilenmiş. {old_seconds}s yerine {last_seconds}s'den başlanacak.")
            else:
                last_seconds = 0

        last_url = target_stream_url
        last_title = film_title
        write_title_file(film_title)

        probe_url = probe_url_of(target_stream_url)
        total_duration_sec = _duration_cache.pop(probe_url, 0.0)
        if total_duration_sec <= 0:
            total_duration_sec = get_video_duration_ffprobe(probe_url)

        print("=" * 60)
        print("📺 Maxanimasyon Canlı Aktarım Yayını Başlatılıyor")
        print(f"🎬 Oynatılan İçerik  : {film_title}")
        print(f"⏱️ Başlangıç Saniyesi: {last_seconds}")
        print(f"⏱️ Toplam Süre      : {format_hms(total_duration_sec) if total_duration_sec > 0 else 'Bilinmiyor'}")

        headers_arg = f"User-Agent: {STREAM_USER_AGENT}\r\nReferer: https://vidmody.com/\r\nOrigin: https://vidmody.com\r\n"

        input_options = [
            '-re',
            '-headers', headers_arg,
            '-protocol_whitelist', 'file,http,https,tcp,tls,crypto',
            '-err_detect', 'ignore_err',
            '-fflags', '+genpts+discardcorrupt',
            '-thread_queue_size', '1024',
            '-max_interleave_delta', '0',
            '-analyzeduration', '10000000',
            '-probesize', '10000000',
            '-reconnect', '1',
            '-reconnect_at_eof', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '10',
            '-rw_timeout', '10000000',
            '-threads', DECODER_THREADS,
        ]

        seek_args = ['-ss', str(last_seconds)] if last_seconds > 0 else []

        if ";" in target_stream_url:
            v_url, a_url = target_stream_url.split(";", 1)
            input_args = input_options + seek_args + ['-i', v_url.strip()] + input_options + seek_args + ['-i', a_url.strip()]
            audio_map = ['-map', '1:a:0?']
            logo1_input_index = 2
        else:
            input_args = input_options + seek_args + ['-i', target_stream_url]
            audio_map = ['-map', '0:a:0?']
            logo1_input_index = 1

        print_dashboard(film_title, current_index, len(playlist), last_seconds, status="🟡 Başlatılıyor")

        has_logo1 = os.path.exists('logo.png') and os.path.getsize('logo.png') > 0
        title_drawtext = f"drawtext=textfile='title.txt':reload=1:fontfile='{BOLD_FONT_PATH}':fontcolor=white@{TEXT_OPACITY}:fontsize=19:x=w-tw-20:y=h-th-20"

        if total_duration_sec > 0:
            base_remaining = max(0.0, total_duration_sec - last_seconds)
            rem_expr = f"ceil(max(0\\,{base_remaining:.3f}-t))"
        else:
            rem_expr = f"floor({float(last_seconds):.3f}+t)"

        time_text = f"%{{eif\\:floor({rem_expr}/3600)\\:d\\:2}}\\:%{{eif\\:mod(floor({rem_expr}/60)\\,60)\\:d\\:2}}\\:%{{eif\\:mod({rem_expr}\\,60)\\:d\\:2}}"
        time_drawtext = f"drawtext=text='{time_text}':fontfile='{BOLD_FONT_PATH}':fontcolor=white@{TEXT_OPACITY}:fontsize=18:x=20:y=h-th-20"

        if has_logo1:
            logo_inputs = ['-i', 'logo.png']
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=25[main];'
                f'[{logo1_input_index}:v]scale=-2:85,format=rgba,colorchannelmixer=aa={LOGO_OPACITY}[logo1];'
                '[main][logo1]overlay=50:50[tmp1];'
                f'[tmp1]{title_drawtext}[tmp2];'
                f'[tmp2]{time_drawtext}[v]'
            )
        else:
            logo_inputs = []
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=25[main];'
                f'[main]{title_drawtext}[tmp2];'
                f'[tmp2]{time_drawtext}[v]'
            )

        publisher.ensure_running()
        ts_offset = publisher.clock() + 1.0

        command = ['ffmpeg'] + input_args + logo_inputs + [
            '-filter_complex', filter_str,
            '-map', '[v]'
        ] + audio_map + [
            '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
            '-r', '25', '-b:v', '2500k', '-maxrate', '2500k', '-bufsize', '3000k',
            '-g', '50', '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '44100',
            '-output_ts_offset', f'{ts_offset:.3f}', '-flush_packets', '1',
            '-f', 'mpegts', 'pipe:1'
        ]

        process = subprocess.Popen(
            command,
            stdout=publisher.proc.stdin,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        if len(playlist) > 0:
            next_item = playlist[(current_index + 1) % len(playlist)]
            threading.Thread(target=prefetch_duration, args=(next_item["url"],), daemon=True).start()

        last_save_time = time.time()
        last_dashboard_time = time.time()
        current_stream_seconds = last_seconds
        stderr_tail = deque(maxlen=40)

        # WATCHDOG Mantığı (Başlangıç ve Normal Yayın Toleranslı)
        last_progress_time = [time.time()]
        has_started_progress = [False]

        def _watchdog(proc=process, progress_ref=last_progress_time, started_ref=has_started_progress):
            while proc.poll() is None:
                time.sleep(5)
                timeout = WATCHDOG_TIMEOUT_SECONDS if started_ref[0] else WATCHDOG_STARTUP_SECONDS
                if time.time() - progress_ref[0] > timeout:
                    print(f"🚨 Watchdog: {timeout} saniyedir veri akışı yok! Süreç kilitlendi, sonlandırılıyor.")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break

        threading.Thread(target=_watchdog, daemon=True).start()

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
                    last_progress_time[0] = now
                    has_started_progress[0] = True

                    if now - last_save_time > 30:
                        update_local_state(current_index, current_stream_seconds, target_stream_url, film_title)
                        last_save_time = now

                    if now - last_dashboard_time > 30:
                        print_dashboard(film_title, current_index, len(playlist), current_stream_seconds)
                        write_step_summary(film_title, current_index, len(playlist), current_stream_seconds)
                        last_dashboard_time = now

        # ===================== GELİŞMİŞ BİTİŞ VE KOPMA KONTROLÜ =====================
        finished_naturally = (process.returncode == 0) and is_really_finished(current_stream_seconds, total_duration_sec)

        if finished_naturally:
            print("✅ İçerik gerçekten bitti, sonraki videoya geçiliyor.")
            write_step_summary(film_title, current_index, len(playlist), current_stream_seconds, status="✅ Bitti, sıradakine geçiliyor")
            current_index += 1
            last_seconds = 0
            last_url = ""
            last_title = ""
            update_local_state(current_index, 0, "", "")
            consecutive_fast_failures = 0
        else:
            # Erken kapanma veya kopma durumu
            if process.returncode == 0:
                print(f"⚠️ FFmpeg 0 kodu verdi ancak film tamamlanmadı ({format_hms(current_stream_seconds)} / {format_hms(total_duration_sec)}). Erken kopma sayıldı!")
            elif process.returncode == -9:
                print("⚠️ FFmpeg watchdog (donma) nedeniyle sonlandırıldı.")

            write_step_summary(film_title, current_index, len(playlist), current_stream_seconds, status="🔴 Bağlantı koptu, tekrar denenecek")

            duration_this_attempt = current_stream_seconds - last_seconds
            if duration_this_attempt < FAST_FAIL_THRESHOLD_SECONDS:
                consecutive_fast_failures += 1
            else:
                consecutive_fast_failures = 0

            # 3 Kez üst üste hızlı çökme yaşanırsa sonraki videoya geç
            if consecutive_fast_failures >= 3:
                print(f"❌ {film_title} akışı sürekli hataya düştü. Sonraki içeriğe atlanıyor...")
                current_index += 1
                last_seconds = 0
                last_url = ""
                last_title = ""
                consecutive_fast_failures = 0
                update_local_state(current_index, 0, "", "")
            else:
                # SEEK BACKOFF: Eğer sürekli aynı noktada çöküyorsa bozuk kareyi (corrupt frame) atlamak için biraz geri sar
                if consecutive_fast_failures > 1 and current_stream_seconds > SEEK_BACKOFF_SECONDS:
                    last_seconds = max(0, current_stream_seconds - SEEK_BACKOFF_SECONDS)
                    print(f"⏪ Takılma koruması: {SEEK_BACKOFF_SECONDS} saniye geriye sarılarak ({last_seconds}s) tekrar denenecek.")
                else:
                    last_seconds = current_stream_seconds

                last_url = target_stream_url
                update_local_state(current_index, last_seconds, last_url, film_title)

        retry_delay = 0 if finished_naturally else (min(5 * (2 ** consecutive_fast_failures), MAX_RETRY_DELAY_SECONDS) if consecutive_fast_failures > 0 else 5)
        if retry_delay > 0:
            print(f"⚠️ {retry_delay} saniye sonra tekrar bağlanılıyor...")
            time.sleep(retry_delay)


if __name__ == "__main__":
    try:
        start_m3u_stream()
    except KeyboardInterrupt:
        print("\n🛑 Durduruluyor...")
    finally:
        publisher.stop()
