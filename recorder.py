import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding='utf-8')

# Target Server & Channels (FLOXI INDORI ARMY)
GUILD_ID = "1429959255309684798"
TARGET_CHANNELS = [
    ("FLOXI ARMY", "1429959256651595930"),
    ("FLOXI ARMY 4", "1529963618580562040"),
    ("floxi 3", "1550210403248771194"),
    ("mods meeting", "1524287475940462694"),
    ("inner circle", "1553496804211818576"),
    ("ic", "1565009474773848124"),
]

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip().strip('"').strip("'")
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "13500"))  # 3h 45m shift
RECORDINGS_DIR = "recordings"
SCREENSHOT_DIR = "screenshots"

os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
stop_requested = False


def handle_sigterm(signum, frame):
    global stop_requested
    print(f"[SIGNAL] Received signal {signum}, initiating graceful shutdown...")
    stop_requested = True


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


def start_ffmpeg(output_path: str) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-y",
        "-thread_queue_size", "4096",
        "-video_size", "1920x1080",
        "-framerate", "30",
        "-draw_mouse", "0",
        "-f", "x11grab", "-i", ":99.0",
        "-thread_queue_size", "4096",
        "-f", "pulse", "-i", "DiscordSink.monitor",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-maxrate", "4000k",
        "-bufsize", "8000k",
        "-pix_fmt", "yuv420p",
        "-af", "aresample=async=1:first_pts=0",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        output_path,
    ]
    print(f"[FFMPEG] Starting capture -> {output_path}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def stop_ffmpeg(proc: subprocess.Popen):
    if proc and proc.poll() is None:
        print("[FFMPEG] Sending 'q' to finalize container safely...")
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
            proc.wait(timeout=15)
        except Exception as e:
            print(f"[FFMPEG] Graceful stop timed out ({e}), terminating...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


async def expand_all_categories(page):
    """Expands any collapsed voice/text categories so sidebar virtualization doesn't hide channels."""
    try:
        await page.evaluate("""
            () => {
                const collapsed = document.querySelectorAll('[aria-expanded="false"]');
                collapsed.forEach(el => {
                    if (el.getAttribute('role') === 'button' || el.tagName === 'H3') {
                        el.click();
                    }
                });
            }
        """)
    except Exception as e:
        print(f"[WARN] Category expand warning: {e}")


async def ensure_self_muted(page):
    """Safely mutes ONLY the user's microphone in the bottom-left user panel (never the stream speaker)."""
    try:
        await page.evaluate("""
            () => {
                const panels = document.querySelector('[class*="panels" i], section[aria-label*="User" i]');
                if (!panels) return;
                const muteBtn = panels.querySelector('button[aria-label="Mute"][role="switch"]');
                if (muteBtn && muteBtn.getAttribute('aria-checked') === 'false') {
                    muteBtn.click();
                }
            }
        """)
    except Exception:
        pass


async def check_channel_live(page, channel_id: str) -> bool:
    """Checks if a target voice channel has a LIVE indicator in the sidebar."""
    try:
        return await page.evaluate(
            """
            (chId) => {
                const link = document.querySelector(`a[href*="/${chId}"]`);
                if (!link) return false;
                link.scrollIntoView({ block: 'center', behavior: 'instant' });
                const container = link.closest('li') || link.parentElement;
                if (!container) return false;
                const textHasLive = /\\bLIVE\\b/i.test(container.textContent || "");
                const badgeHasLive = container.querySelector('[class*="liveBadge" i], [class*="live_" i], [class*="live-" i]') !== null;
                return textHasLive || badgeHasLive;
            }
            """,
            channel_id,
        )
    except Exception as e:
        print(f"[WARN] Error checking channel {channel_id}: {e}")
        return False


async def wait_for_stream_video(page, timeout_sec: int = 25) -> bool:
    """Waits up to timeout_sec for the WebRTC/DAVE video stream to decode frames."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            ready = await page.evaluate("""
                () => {
                    const videos = Array.from(document.querySelectorAll('video'));
                    return videos.some(v => v.readyState >= 2 && !v.paused && v.videoWidth >= 320);
                }
            """)
            if ready:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def leave_stream_and_voice(page):
    """Exits Fullscreen and disconnects from the voice channel cleanly."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        disconnect_btn = page.locator('[aria-label="Disconnect"]').first
        if await disconnect_btn.count() > 0:
            await disconnect_btn.click()
            print("[STREAM] Disconnected from voice channel.")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"[WARN] Error during voice disconnect: {e}")


async def enter_stream_and_fullscreen(page, channel_name: str, channel_id: str) -> bool:
    print(f"[STREAM] Connecting to '{channel_name}' ({channel_id})...")
    link = page.locator(f'a[href*="/{channel_id}"]').first
    if await link.count() == 0:
        return False

    await link.click()
    await asyncio.sleep(3)

    # If Discord opens a voice lobby preview, click 'Join Voice' first and let it connect
    joined_lobby = await page.evaluate("""
        () => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const joinBtn = buttons.find(b => b.textContent && b.textContent.toLowerCase().includes('join voice'));
            if (joinBtn) {
                joinBtn.click();
                return true;
            }
            return false;
        }
    """)
    if joined_lobby:
        await asyncio.sleep(3)

    await ensure_self_muted(page)

    # Attempt up to 4 times to click 'Watch Stream' or a true stream tile (never normal voice avatar tiles)
    watch_clicked = False
    for _ in range(4):
        watch_clicked = await page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const watchBtn = buttons.find(b => b.textContent && b.textContent.toLowerCase().includes('watch stream'));
                if (watchBtn) {
                    watchBtn.click();
                    return true;
                }
                const streamTile = document.querySelector(
                    '[class*="streamPreview" i], [class*="tile" i]:has([class*="live" i])'
                );
                if (streamTile) {
                    streamTile.click();
                    return true;
                }
                return false;
            }
        """)
        if watch_clicked:
            break
        await asyncio.sleep(2)

    print(f"[STREAM] Watch Stream triggered: {watch_clicked}")
    if not watch_clicked:
        print("[STREAM] Could not find Watch Stream button/tile. Disconnecting from VC...")
        await leave_stream_and_voice(page)
        return False

    # Wait for WebRTC/DAVE stream frames to actually start decoding
    video_ready = await wait_for_stream_video(page, timeout_sec=25)
    print(f"[STREAM] Video frames decoding: {video_ready}")
    if not video_ready:
        print("[STREAM] Stream video failed to decode within 25s. Disconnecting from VC...")
        await leave_stream_and_voice(page)
        return False

    # Hover over video briefly so Discord renders the bottom-right Full Screen button
    video_el = page.locator("video").first
    if await video_el.count() > 0:
        try:
            await video_el.hover(position={"x": 200, "y": 200})
            await asyncio.sleep(1)
        except Exception:
            pass

    fs_btn = page.locator('[aria-label="Full Screen"], [aria-label="Fullscreen"]').first
    if await fs_btn.count() > 0:
        await fs_btn.click()
        print("[STREAM] Fullscreen triggered via button.")
    elif await video_el.count() > 0:
        await video_el.dblclick(position={"x": 200, "y": 200})
        print("[STREAM] Fullscreen triggered via safe quadrant dblclick.")

    # Park mouse cursor at (0, 0) so Discord auto-hides bottom control bar & participant HUD
    await page.mouse.move(0, 0)
    await asyncio.sleep(2)
    await page.screenshot(path=f"{SCREENSHOT_DIR}/stream_started_{int(time.time())}.png")
    return True


async def is_stream_still_active(page) -> bool:
    """Verifies that an active stream video element (>=320px width) is still playing."""
    try:
        return await page.evaluate("""
            () => {
                const bodyText = document.body.innerText || "";
                if (bodyText.includes("Stream has ended") || bodyText.includes("stream is currently paused")) {
                    return false;
                }
                const videos = Array.from(document.querySelectorAll('video'));
                return videos.some(v => v.readyState >= 2 && !v.paused && v.videoWidth >= 320);
            }
        """)
    except Exception:
        return False


async def main():
    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_TOKEN environment variable is missing!")
        sys.exit(1)

    start_time = time.time()
    ffmpeg_proc = None
    segment_index = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # Rendered on Xvfb :99
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-dev-shm-usage",
                "--use-fake-ui-for-media-stream",
                "--no-sandbox",
                "--window-position=0,0",
                "--window-size=1920,1080",
                "--start-fullscreen",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            permissions=["microphone", "camera"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )

        # Inject Discord token via both direct localStorage and iframe fallback (survives Discord's boot wipe)
        init_script = f"""
            (() => {{
                const token = JSON.stringify("{DISCORD_TOKEN}");
                try {{
                    window.localStorage.setItem('token', token);
                }} catch (e) {{}}
                window.addEventListener('DOMContentLoaded', () => {{
                    try {{
                        const iframe = document.createElement('iframe');
                        iframe.style.display = 'none';
                        document.body.appendChild(iframe);
                        iframe.contentWindow.localStorage.token = token;
                        iframe.remove();
                    }} catch (e) {{}}
                }});
            }})();
        """
        await context.add_init_script(init_script)

        page = await context.new_page()
        guild_url = f"https://discord.com/channels/{GUILD_ID}/{TARGET_CHANNELS[0][1]}"
        print(f"[INIT] Navigating to guild {GUILD_ID}...")
        try:
            await page.goto(guild_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[WARN] Navigation warning: {e}")

        await asyncio.sleep(10)

        await expand_all_categories(page)
        await ensure_self_muted(page)
        await page.mouse.move(0, 0)
        await page.screenshot(path=f"{SCREENSHOT_DIR}/01_guild_loaded.png")
        print("[MONITOR] Polling 6 voice channels every 5 seconds...")

        try:
            while not stop_requested and (time.time() - start_time) < MAX_RUNTIME_SECONDS:
                await expand_all_categories(page)

                for ch_name, ch_id in TARGET_CHANNELS:
                    if stop_requested or (time.time() - start_time) >= MAX_RUNTIME_SECONDS:
                        break

                    is_live = await check_channel_live(page, ch_id)
                    if is_live:
                        print(f"[DETECTED] LIVE badge found in '{ch_name}' ({ch_id})!")
                        joined = await enter_stream_and_fullscreen(page, ch_name, ch_id)
                        if joined:
                            segment_index += 1
                            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                            safe_name = ch_name.replace(" ", "_")
                            raw_output = f"{RECORDINGS_DIR}/Floxi_Raw_{segment_index:02d}_{safe_name}_{ts}.mp4"

                            ffmpeg_proc = start_ffmpeg(raw_output)
                            empty_checks = 0

                            # Monitor active stream loop
                            while not stop_requested and (time.time() - start_time) < MAX_RUNTIME_SECONDS:
                                await asyncio.sleep(5)
                                await page.mouse.move(0, 0)
                                active = await is_stream_still_active(page)
                                if not active:
                                    empty_checks += 1
                                    print(f"[MONITOR] Stream inactive check {empty_checks}/6")
                                    if empty_checks >= 6:  # 30s grace period for brief streamer lag
                                        print("[MONITOR] Stream ended. Stopping recording...")
                                        break
                                else:
                                    empty_checks = 0

                            stop_ffmpeg(ffmpeg_proc)
                            ffmpeg_proc = None
                            await page.screenshot(path=f"{SCREENSHOT_DIR}/stream_ended_{segment_index:02d}.png")
                            await leave_stream_and_voice(page)

                await asyncio.sleep(5)

        finally:
            if ffmpeg_proc:
                stop_ffmpeg(ffmpeg_proc)
            await browser.close()
            print("[SHUTDOWN] Browser closed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
