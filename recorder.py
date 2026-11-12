import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")

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
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "13200"))  # 3h 40m per shift
FAILED_JOIN_COOLDOWN_SEC = 90
INACTIVE_GRACE_CHECKS = 15  # 15 checks at 5s interval = 75s grace for static screen-shares
MAX_FFMPEG_FAST_CRASHES = 3
MAX_FFMPEG_TOTAL_RESTARTS = 6
MAX_STALLED_FILE_CHECKS = 6  # 6 checks at 5s interval = 30s max with zero bytes written

RECORDINGS_DIR = "recordings"
SCREENSHOT_DIR = "screenshots"
LOGS_DIR = "logs"

for d in (RECORDINGS_DIR, SCREENSHOT_DIR, LOGS_DIR):
    os.makedirs(d, exist_ok=True)

stop_requested = False
last_video_state = {"time": -1.0, "frames": -1}


def handle_sigterm(signum, frame):
    global stop_requested
    print(f"[SIGNAL] Received signal {signum}, initiating graceful shutdown...")
    stop_requested = True


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


async def safe_screenshot(page, path: str):
    """Takes a diagnostic screenshot without raising unhandled exceptions."""
    try:
        await page.screenshot(path=path, timeout=5000)
    except Exception as e:
        print(f"[WARN] Screenshot failed ({path}): {e}")


def verify_pulseaudio_routing():
    """Ensures all active Chromium PulseAudio sink-inputs are unmuted and routed to DiscordSink."""
    try:
        res = subprocess.run(
            ["pactl", "list", "short", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if lines:
            print(f"[AUDIO] Active PulseAudio sink-inputs ({len(lines)}): {lines[0]}")
            for line in lines:
                input_id = line.split()[0]
                subprocess.run(["pactl", "set-sink-input-mute", input_id, "0"], timeout=3)
                subprocess.run(["pactl", "move-sink-input", input_id, "DiscordSink"], timeout=3)
        else:
            print("[AUDIO] No active PulseAudio sink-input yet (stream may be silent or initializing).")
    except Exception as e:
        print(f"[WARN] PulseAudio routing check warning: {e}")


def start_ffmpeg(output_path: str, log_path: str) -> subprocess.Popen:
    log_file = open(log_path, "ab")
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
        "-threads", "2",
        "-crf", "23",
        "-maxrate", "3500k",
        "-bufsize", "7000k",
        "-g", "60",
        "-fps_mode", "cfr",
        "-pix_fmt", "yuv420p",
        "-af", "aresample=async=1:first_pts=0",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        output_path,
    ]
    print(f"[FFMPEG] Starting capture -> {output_path} (Log: {log_path})")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log_file = log_file
    proc._output_path = output_path
    proc._start_ts = time.time()
    return proc


def stop_ffmpeg(proc: subprocess.Popen):
    if not proc:
        return
    if proc.poll() is None:
        print("[FFMPEG] Sending 'q' to finalize container safely...")
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
            proc.wait(timeout=12)
        except Exception as e:
            print(f"[FFMPEG] Graceful stop timed out ({e}), terminating...")
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
    else:
        print(f"[FFMPEG] Process already exited with code {proc.returncode}")

    if hasattr(proc, "_log_file") and proc._log_file:
        try:
            proc._log_file.close()
        except Exception:
            pass

    out_file = getattr(proc, "_output_path", None)
    if out_file and os.path.exists(out_file):
        try:
            size_bytes = os.path.getsize(out_file)
            if size_bytes < 65536:
                print(f"[CLEANUP] Removing tiny/corrupt segment {out_file} ({size_bytes} bytes)")
                os.remove(out_file)
            else:
                print(f"[FFMPEG] Finalized segment {out_file} ({size_bytes // 1024} KB)")
        except Exception:
            pass


async def verify_discord_login(page, timeout_sec: int = 35) -> bool:
    """Verifies that authentication succeeded AND the target guild's channel list loaded."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        url = page.url
        if "/login" not in url:
            guild_loaded = await page.evaluate(
                """
                (guildId) => {
                    const hasGuildLink = document.querySelector(`a[href*="/channels/${guildId}/"]`) !== null;
                    const hasChannelsNav = document.querySelector('#channels, nav[aria-label*="channel" i]') !== null;
                    return hasGuildLink && hasChannelsNav;
                }
                """,
                GUILD_ID,
            )
            if guild_loaded:
                return True
        await asyncio.sleep(1)
    return False


async def dismiss_modals_and_expand_categories(page):
    """Dismisses Discord promo/quest modals and expands ONLY collapsed channel categories (never server header)."""
    try:
        if await page.locator('[role="dialog"]').count() > 0:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

        await page.evaluate("""
            () => {
                const scroller = document.querySelector('#channels [class*="scroller" i], nav [class*="scroller" i]');
                if (!scroller) return;
                scroller.querySelectorAll('[aria-expanded="false"]').forEach(el => {
                    const isCategory = el.tagName === 'H3' ||
                        el.closest('[class*="containerDefault" i], [class*="category" i], [class*="section" i]') !== null;
                    const isHeader = el.closest('header') !== null;
                    if (isCategory && !isHeader) {
                        el.click();
                    }
                });
            }
        """)
    except Exception as e:
        print(f"[WARN] Modal/Category cleanup warning: {e}")


async def ensure_self_muted(page):
    """Safely mutes ONLY the bot's microphone and ensures Deafen is OFF so incoming audio is captured."""
    try:
        await page.evaluate("""
            () => {
                const panels = document.querySelector('section[aria-label*="User" i], [class*="panels" i]');
                if (!panels) return;
                const muteBtn = panels.querySelector('button[aria-label="Mute"][role="switch"]');
                if (muteBtn && muteBtn.getAttribute('aria-checked') === 'false') {
                    muteBtn.click();
                }
                const deafenBtn = panels.querySelector('button[aria-label="Deafen"][role="switch"]');
                if (deafenBtn && deafenBtn.getAttribute('aria-checked') === 'true') {
                    deafenBtn.click();
                }
            }
        """)
    except Exception:
        pass


async def scan_all_channels_in_sidebar(page, channel_ids: list[str]) -> dict[str, dict]:
    """Sweeps the virtualized sidebar once and checks LIVE badge + voice participant count for all target channels."""
    try:
        return await page.evaluate(
            """
            async (chIds) => {
                const results = {};
                chIds.forEach(id => { results[id] = { live: false, users: 0 }; });
                const targetSet = new Set(chIds);

                const checkVisible = () => {
                    const scroller = document.querySelector('#channels [class*="scroller" i], nav [class*="scroller" i]') || document;
                    let lastChId = null;
                    const nodes = scroller.querySelectorAll('li, [class*="containerDefault" i], [class*="voiceUser" i]');
                    for (const node of nodes) {
                        if (node.matches('[class*="voiceUser" i]')) {
                            if (lastChId && targetSet.has(lastChId)) {
                                const txt = node.innerText || '';
                                if (/\\bLIVE\\b/.test(txt) || node.querySelector('[class*="liveBadge" i], [class*="live_" i]')) {
                                    results[lastChId].live = true;
                                }
                                results[lastChId].users = Math.max(results[lastChId].users, 1);
                            }
                            continue;
                        }
                        const link = node.querySelector('a[href*="/channels/"]');
                        if (!link) continue;
                        const href = link.getAttribute('href') || '';
                        const matchedId = chIds.find(id => href.includes('/' + id));
                        lastChId = matchedId || null;
                        if (!matchedId) continue;

                        const badgeHasLive = node.querySelector(
                            '[class*="liveBadge" i], [class*="liveIndicator" i], [class*="live_" i], [class*="liveSmall" i]'
                        ) !== null;
                        const textHasLive = /\\bLIVE\\b/.test(node.innerText || '');
                        if (badgeHasLive || textHasLive) {
                            results[matchedId].live = true;
                        }
                        const vUsers = node.querySelectorAll('[class*="voiceUser" i]').length;
                        if (vUsers > results[matchedId].users) {
                            results[matchedId].users = vUsers;
                        }
                        const aria = (link.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('aria-label') || '');
                        const ariaMatch = aria.match(/(\\d+)\\s+(?:user|people|member)/i);
                        if (ariaMatch) {
                            const cnt = parseInt(ariaMatch[1], 10);
                            if (cnt > results[matchedId].users) results[matchedId].users = cnt;
                        }
                        const pillMatch = (link.innerText || '').match(/\\b(\\d+)\\s*\\n\\s*\\d+\\b/);
                        if (pillMatch) {
                            const cnt = parseInt(pillMatch[1], 10);
                            if (cnt > results[matchedId].users) results[matchedId].users = cnt;
                        }
                    }
                };

                checkVisible();
                const scroller = document.querySelector('#channels [class*="scroller" i], nav [class*="scroller" i]');
                if (!scroller) return results;

                scroller.scrollTop = 0;
                await new Promise(r => setTimeout(r, 80));
                const maxScroll = scroller.scrollHeight;
                for (let pos = 0; pos <= maxScroll; pos += 300) {
                    scroller.scrollTop = pos;
                    await new Promise(r => setTimeout(r, 60));
                    checkVisible();
                }
                scroller.scrollTop = 0;
                return results;
            }
            """,
            channel_ids,
        )
    except Exception as e:
        print(f"[WARN] Error during batch sidebar scan: {e}")
        return {cid: {"live": False, "users": 0} for cid in channel_ids}


async def scroll_channel_into_view(page, channel_id: str) -> bool:
    """Scrolls a specific channel link into the viewport before clicking."""
    try:
        return await page.evaluate(
            """
            async (chId) => {
                const scroller = document.querySelector('#channels [class*="scroller" i], nav [class*="scroller" i]');
                const tryFocus = () => {
                    const link = document.querySelector(`a[href*="/${chId}"]`);
                    if (!link) return false;
                    link.scrollIntoView({ block: 'center', behavior: 'instant' });
                    return true;
                };
                if (tryFocus() || !scroller) return tryFocus();
                scroller.scrollTop = 0;
                for (let pos = 0; pos <= scroller.scrollHeight; pos += 350) {
                    scroller.scrollTop = pos;
                    await new Promise(r => setTimeout(r, 60));
                    if (tryFocus()) return true;
                }
                return false;
            }
            """,
            channel_id,
        )
    except Exception:
        return False


async def get_active_stream_video_state(page) -> dict:
    """Finds the active stream video (verifying WebRTC MediaStreamTrack + prioritizing stream tiles over webcams)."""
    try:
        return await page.evaluate("""
            () => {
                const allVideos = Array.from(document.querySelectorAll('video'))
                    .filter(v => {
                        if (v.readyState < 2 || v.paused || v.videoWidth < 320) return false;
                        if (v.srcObject && typeof v.srcObject.getVideoTracks === 'function') {
                            const tracks = v.srcObject.getVideoTracks();
                            if (tracks.length > 0 && !tracks.some(t => t.readyState === 'live')) {
                                return false;
                            }
                        }
                        return true;
                    });

                if (!allVideos.length) {
                    return { active: false, currentTime: -1, frames: -1, box: null };
                }

                const scored = allVideos.map(v => {
                    const tile = v.closest('[class*="tile" i], [class*="videoWrapper" i], [class*="stream" i]');
                    const isStreamTile = tile ? (
                        tile.querySelector('[class*="liveBadge" i], [class*="liveIndicator" i], [class*="live_" i]') !== null ||
                        /\\bLIVE\\b/.test(tile.innerText || "")
                    ) : false;
                    const area = Math.imul(v.videoWidth, v.videoHeight);
                    const score = (isStreamTile ? 100000000 : 0) + area;
                    return { v, score };
                });

                scored.sort((a, b) => b.score - a.score);
                const mainVid = scored[0].v;

                const wrapper = mainVid.parentElement ? mainVid.parentElement.parentElement : null;
                const stageText = wrapper ? (wrapper.innerText || "") : "";
                if (stageText.includes("Stream has ended") || stageText.includes("stream is currently paused")) {
                    return { active: false, currentTime: -1, frames: -1, box: null };
                }

                const q = mainVid.getVideoPlaybackQuality ? mainVid.getVideoPlaybackQuality() : null;
                const frames = q ? q.totalVideoFrames : 1;
                const r = mainVid.getBoundingClientRect();
                const box = (r.width >= 100 && r.height >= 100)
                    ? { x: r.left + Math.min(200, r.width / 4), y: r.top + Math.min(200, r.height / 4) }
                    : null;

                return {
                    active: mainVid.currentTime > 0 && frames > 0,
                    currentTime: mainVid.currentTime,
                    frames: frames,
                    box: box
                };
            }
        """)
    except Exception:
        return {"active": False, "currentTime": -1, "frames": -1, "box": None}


async def wait_for_stream_video(page, timeout_sec: int = 25) -> dict:
    """Waits up to timeout_sec for the primary stream video to decode frames."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline and not stop_requested:
        state = await get_active_stream_video_state(page)
        if state.get("active"):
            return state
        await asyncio.sleep(1)
    return {"active": False, "currentTime": -1, "frames": -1, "box": None}


async def leave_stream_and_voice(page, guild_url: str = ""):
    """Exits Fullscreen, clicks Disconnect, and verifies voice disconnection (with hard WebRTC reset fallback)."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        for _ in range(3):
            await page.mouse.move(960, 1000)
            await asyncio.sleep(0.5)

            still_in_vc = await page.evaluate("""
                () => {
                    const btn = document.querySelector('[aria-label="Disconnect"]');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """)
            if not still_in_vc:
                break
            print("[STREAM] Clicked Disconnect button, verifying state...")
            await asyncio.sleep(1.5)

        remaining = await page.locator('[aria-label="Disconnect"]').count()
        if remaining == 0:
            print("[STREAM] Confirmed voice channel disconnection.")
        elif guild_url:
            print("[WARN] Disconnect button still present! Forcing WebRTC teardown via page reset...")
            await page.goto("about:blank", timeout=10000)
            await asyncio.sleep(1)
            await page.goto(guild_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(6)

        await page.mouse.move(0, 0)
    except Exception as e:
        print(f"[WARN] Error during voice disconnect: {e}")


async def join_voice_channel(page, channel_name: str, channel_id: str, guild_url: str) -> bool:
    """Clicks a voice channel, joins voice (if lobby shown), verifies Disconnect button, and routes PulseAudio."""
    print(f"[VC] Connecting to '{channel_name}' ({channel_id})...")
    if not await scroll_channel_into_view(page, channel_id):
        print(f"[WARN] Channel {channel_name} not found in sidebar.")
        return False

    link = page.locator(f'a[href*="/{channel_id}"]').first
    try:
        await link.click(timeout=10000)
    except Exception as e:
        print(f"[WARN] Failed to click channel link: {e}")
        return False

    await asyncio.sleep(3)

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

    # Verify we actually connected to the VC (Disconnect control visible)
    connected = False
    for _ in range(5):
        if await page.locator('[aria-label="Disconnect"]').count() > 0:
            connected = True
            break
        await asyncio.sleep(1)

    if not connected:
        print(f"[WARN] Could not verify VC connection for '{channel_name}'.")
        return False

    await ensure_self_muted(page)
    verify_pulseaudio_routing()
    return True


async def count_vc_participants(page, channel_id: str) -> int:
    """Counts total participants currently visible in the connected VC (stage tiles or sidebar users)."""
    try:
        return await page.evaluate(
            """
            (chId) => {
                let best = 0;
                const callStage = document.querySelector('main, [class*="callContainer" i]');
                if (callStage) {
                    const tiles = callStage.querySelectorAll('[class*="tile" i][data-selenium-video-tile], [class*="callContainer" i] [class*="tile" i]');
                    if (tiles.length > best) best = tiles.length;
                }
                const link = document.querySelector(`a[href*="/${chId}"]`);
                if (link) {
                    const node = link.closest('li, [class*="containerDefault" i]') || link.parentElement;
                    if (node) {
                        const vUsers = node.querySelectorAll('[class*="voiceUser" i]').length;
                        if (vUsers > best) best = vUsers;
                        const aria = (link.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('aria-label') || '');
                        const m = aria.match(/(\\d+)\\s+(?:user|people|member)/i);
                        if (m) best = Math.max(best, parseInt(m[1], 10));
                        const pill = (link.innerText || '').match(/\\b(\\d+)\\s*\\n\\s*\\d+\\b/);
                        if (pill) best = Math.max(best, parseInt(pill[1], 10));
                    }
                }
                return best;
            }
            """,
            channel_id,
        )
    except Exception:
        return 0


async def try_watch_stream(page, timeout_sec: int = 12, take_screenshot: bool = True) -> bool:
    """Tries to open an active screen-share stream inside the current VC and enter Fullscreen."""
    global last_video_state
    pre_state = await get_active_stream_video_state(page)
    watch_clicked = pre_state.get("active", False)
    if watch_clicked:
        print("[STREAM] Stream auto-started upon joining VC!")

    attempts = max(1, timeout_sec // 3)
    for _ in range(attempts):
        if watch_clicked:
            break
        watch_clicked = await page.evaluate("""
            () => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const watchBtn = buttons.find(b => b.textContent && b.textContent.toLowerCase().includes('watch stream'));
                if (watchBtn) {
                    watchBtn.click();
                    return true;
                }
                const streamTile = document.querySelector(
                    '[class*="streamPreview" i], [class*="tile" i]:has([class*="liveBadge" i]), [class*="tile" i]:has([class*="liveIndicator" i])'
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
        check_now = await get_active_stream_video_state(page)
        if check_now.get("active"):
            watch_clicked = True
            break
        await asyncio.sleep(2)

    if not watch_clicked:
        return False

    video_state = await wait_for_stream_video(page, timeout_sec=timeout_sec)
    if not video_state.get("active", False):
        return False

    last_video_state = {"time": -1.0, "frames": -1}
    verify_pulseaudio_routing()

    try:
        box = video_state.get("box")
        if box:
            await page.mouse.move(box["x"], box["y"])
            await asyncio.sleep(1)

        fs_btn = page.locator('[aria-label="Full Screen"], [aria-label="Fullscreen"]').first
        if await fs_btn.count() > 0:
            await fs_btn.click(timeout=5000)
            print("[STREAM] Fullscreen triggered via button.")
        elif box:
            await page.mouse.dblclick(box["x"], box["y"])
            print("[STREAM] Fullscreen triggered via primary stream video safe quadrant dblclick.")
    except Exception as e:
        print(f"[WARN] Fullscreen trigger warning: {e}")

    await page.mouse.move(0, 0)
    await asyncio.sleep(1.5)
    if take_screenshot:
        await safe_screenshot(page, f"{SCREENSHOT_DIR}/stream_started_{int(time.time())}.png")
    return True


async def is_stream_still_active(page) -> bool:
    """Checks WebRTC track status, currentTime, and totalVideoFrames on the primary playing stream video."""
    global last_video_state
    state = await get_active_stream_video_state(page)
    if not state.get("active"):
        return False

    curr_t = state["currentTime"]
    curr_f = state["frames"]

    time_stuck = last_video_state["time"] >= 0 and abs(curr_t - last_video_state["time"]) < 0.001
    frames_stuck = last_video_state["frames"] >= 0 and curr_f >= 0 and curr_f == last_video_state["frames"]

    last_video_state = {"time": curr_t, "frames": curr_f}

    if time_stuck and frames_stuck:
        return False
    return True


async def main():
    if not DISCORD_TOKEN:
        print("[ERROR] DISCORD_TOKEN environment variable is missing!")
        sys.exit(1)

    start_time = time.time()
    ffmpeg_proc = None
    segment_index = 0
    failed_cooldowns = {}
    consecutive_loop_errors = 0
    all_channel_ids = [cid for _, cid in TARGET_CHANNELS]
    guild_url = f"https://discord.com/channels/{GUILD_ID}/{TARGET_CHANNELS[0][1]}"
    safe_token_json = json.dumps(DISCORD_TOKEN)

    async with async_playwright() as p:
        launch_args = [
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--enable-unsafe-swiftshader",
            "--disable-gpu-sandbox",
            "--use-fake-ui-for-media-stream",
            "--no-sandbox",
            "--window-position=0,0",
            "--window-size=1920,1080",
            "--start-fullscreen",
        ]
        try:
            browser = await p.chromium.launch(headless=False, args=launch_args)
        except Exception as launch_err:
            print(f"[WARN] Bundled Chromium launch failed ({launch_err}), falling back to system Chrome...")
            browser = await p.chromium.launch(channel="chrome", headless=False, args=launch_args)

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            permissions=["microphone", "camera"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )

        init_script = f"""
            (() => {{
                const token = JSON.stringify({safe_token_json});
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
        print(f"[INIT] Navigating to guild {GUILD_ID}...")
        try:
            await page.goto(guild_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[WARN] Initial navigation warning: {e}")

        if not await verify_discord_login(page, timeout_sec=35):
            await safe_screenshot(page, f"{SCREENSHOT_DIR}/00_LOGIN_FAILED.png")
            print(f"[FATAL] Discord login or guild load failed (URL: {page.url}). Check 00_LOGIN_FAILED.png!")
            await browser.close()
            sys.exit(1)

        await asyncio.sleep(5)
        await dismiss_modals_and_expand_categories(page)
        await ensure_self_muted(page)
        await page.mouse.move(0, 0)
        await safe_screenshot(page, f"{SCREENSHOT_DIR}/01_guild_loaded.png")
        print("[MONITOR] Login & Guild verified. Polling 6 voice channels (LIVE streams + active voice calls) every 5s...")

        try:
            while not stop_requested and (time.time() - start_time) < MAX_RUNTIME_SECONDS:
                try:
                    await dismiss_modals_and_expand_categories(page)
                    chan_map = await scan_all_channels_in_sidebar(page, all_channel_ids)
                    now_ts = time.time()

                    # Priority 1: Target channel with an active LIVE screen-share stream
                    # Priority 2: Target channel with active voice participants (most users first)
                    chosen = None
                    for ch_name, ch_id in TARGET_CHANNELS:
                        if now_ts < failed_cooldowns.get(ch_id, 0):
                            continue
                        info = chan_map.get(ch_id) or {}
                        if info.get("live"):
                            chosen = (ch_name, ch_id, "stream", info.get("users", 1))
                            break

                    if not chosen:
                        voice_candidates = []
                        for ch_name, ch_id in TARGET_CHANNELS:
                            if now_ts < failed_cooldowns.get(ch_id, 0):
                                continue
                            info = chan_map.get(ch_id) or {}
                            users = int(info.get("users", 0) or 0)
                            if users >= 1:
                                voice_candidates.append((users, ch_name, ch_id))
                        if voice_candidates:
                            voice_candidates.sort(key=lambda x: x[0], reverse=True)
                            users, ch_name, ch_id = voice_candidates[0]
                            chosen = (ch_name, ch_id, "voice", users)

                    if chosen:
                        ch_name, ch_id, want_mode, user_hint = chosen
                        print(f"[DETECTED] '{ch_name}' ({ch_id}) -> mode={want_mode} (users={user_hint})")
                        vc_ok = await join_voice_channel(page, ch_name, ch_id, guild_url)
                        if not vc_ok:
                            print(f"[COOLDOWN] Putting '{ch_name}' on {FAILED_JOIN_COOLDOWN_SEC}s cooldown.")
                            failed_cooldowns[ch_id] = time.time() + FAILED_JOIN_COOLDOWN_SEC
                            continue

                        stream_ok = await try_watch_stream(page, timeout_sec=(20 if want_mode == "stream" else 6))
                        if stream_ok:
                            active_mode = "stream"
                        else:
                            part_count = await count_vc_participants(page, ch_id)
                            if part_count < 2 and user_hint < 1:
                                print(f"[VC] '{ch_name}' appears empty after join (participants={part_count}). Leaving...")
                                await leave_stream_and_voice(page, guild_url)
                                failed_cooldowns[ch_id] = time.time() + 30
                                continue
                            active_mode = "voice"
                            await safe_screenshot(page, f"{SCREENSHOT_DIR}/voice_started_{int(time.time())}.png")
                            print(f"[VOICE] Active voice call captured in '{ch_name}' (participants={part_count}, hint={user_hint})!")

                        segment_index += 1
                        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                        safe_name = ch_name.replace(" ", "_")
                        raw_output = f"{RECORDINGS_DIR}/Floxi_Raw_{segment_index:02d}_{active_mode}_{safe_name}_{ts}.mp4"
                        ff_log = f"{LOGS_DIR}/ffmpeg_{segment_index:02d}_{active_mode}_{safe_name}_{ts}.log"

                        ffmpeg_proc = start_ffmpeg(raw_output, ff_log)
                        empty_checks = 0
                        alone_checks = 0
                        audio_routed_pass2 = False
                        scan_tick = 0
                        ffmpeg_fast_crashes = 0
                        ffmpeg_total_restarts = 0
                        last_file_size = 0
                        stalled_size_checks = 0

                        while not stop_requested and (time.time() - start_time) < MAX_RUNTIME_SECONDS:
                            await asyncio.sleep(5)
                            await page.mouse.move(0, 0)
                            scan_tick += 1

                            if not audio_routed_pass2 and scan_tick >= 2:
                                verify_pulseaudio_routing()
                                audio_routed_pass2 = True

                            proc_exited = ffmpeg_proc.poll() is not None
                            curr_size = os.path.getsize(raw_output) if os.path.exists(raw_output) else 0
                            uptime = time.time() - getattr(ffmpeg_proc, "_start_ts", time.time())

                            if not proc_exited and uptime > 15.0:
                                if curr_size <= last_file_size:
                                    stalled_size_checks += 1
                                    print(f"[WARN] MP4 file size not growing ({curr_size} bytes) check {stalled_size_checks}/{MAX_STALLED_FILE_CHECKS}")
                                else:
                                    stalled_size_checks = 0
                                    last_file_size = curr_size

                            if proc_exited or stalled_size_checks >= MAX_STALLED_FILE_CHECKS:
                                reason = f"exited code {ffmpeg_proc.returncode}" if proc_exited else "stalled output growth for 30s"
                                print(f"[ERROR] FFmpeg {reason} (uptime {uptime:.1f}s)!")
                                stop_ffmpeg(ffmpeg_proc)
                                ffmpeg_proc = None

                                ffmpeg_total_restarts += 1
                                if ffmpeg_total_restarts > MAX_FFMPEG_TOTAL_RESTARTS:
                                    print(f"[FATAL] Exceeded max FFmpeg restarts ({MAX_FFMPEG_TOTAL_RESTARTS}) for this session! Aborting.")
                                    failed_cooldowns[ch_id] = time.time() + FAILED_JOIN_COOLDOWN_SEC
                                    break

                                if uptime < 20.0:
                                    ffmpeg_fast_crashes += 1
                                    if ffmpeg_fast_crashes >= MAX_FFMPEG_FAST_CRASHES:
                                        print("[FATAL] FFmpeg crashed 3 times rapidly! Aborting stream session.")
                                        failed_cooldowns[ch_id] = time.time() + FAILED_JOIN_COOLDOWN_SEC
                                        break
                                    backoff_sec = ffmpeg_fast_crashes + ffmpeg_fast_crashes + ffmpeg_fast_crashes
                                    await asyncio.sleep(backoff_sec)
                                else:
                                    ffmpeg_fast_crashes = 0

                                segment_index += 1
                                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                                raw_output = f"{RECORDINGS_DIR}/Floxi_Raw_{segment_index:02d}_{active_mode}_{safe_name}_{ts}.mp4"
                                ff_log = f"{LOGS_DIR}/ffmpeg_{segment_index:02d}_{active_mode}_{safe_name}_{ts}.log"
                                ffmpeg_proc = start_ffmpeg(raw_output, ff_log)
                                last_file_size = 0
                                stalled_size_checks = 0

                            if active_mode == "stream":
                                active = await is_stream_still_active(page)
                                if not active:
                                    empty_checks += 1
                                    print(f"[MONITOR] Stream inactive/frozen check {empty_checks}/{INACTIVE_GRACE_CHECKS}")
                                    if empty_checks >= INACTIVE_GRACE_CHECKS:
                                        part_count = await count_vc_participants(page, ch_id)
                                        if part_count >= 2:
                                            print(f"[VOICE] Screen-share ended but {part_count} users still in VC — continuing in voice mode!")
                                            active_mode = "voice"
                                            empty_checks = 0
                                            await page.keyboard.press("Escape")
                                        else:
                                            print("[MONITOR] Stream & VC ended. Stopping recording...")
                                            break
                                else:
                                    empty_checks = 0
                            else:
                                # Voice mode: check if screen-share started in current VC
                                if await try_watch_stream(page, timeout_sec=3, take_screenshot=True):
                                    print("[STREAM] Live stream started inside active VC! Upgraded voice -> stream.")
                                    active_mode = "stream"
                                    alone_checks = 0
                                    continue

                                # Check every 20s if a LIVE screen-share appeared in another target VC
                                if scan_tick % 4 == 0:
                                    other_map = await scan_all_channels_in_sidebar(page, all_channel_ids)
                                    preempt = any(
                                        cid != ch_id and (other_map.get(cid) or {}).get("live")
                                        and time.time() >= failed_cooldowns.get(cid, 0)
                                        for _, cid in TARGET_CHANNELS
                                    )
                                    if preempt:
                                        print("[PREEMPT] Another target channel started a LIVE screen-share! Switching...")
                                        break

                                # Verify other people are still in this VC (>= 2 including bot)
                                part_count = await count_vc_participants(page, ch_id)
                                if part_count == 1:
                                    alone_checks += 1
                                    print(f"[VOICE] Bot appears alone in VC check {alone_checks}/6")
                                    if alone_checks >= 6:
                                        print("[VOICE] Everyone left VC. Stopping voice recording...")
                                        break
                                else:
                                    alone_checks = 0

                        stop_ffmpeg(ffmpeg_proc)
                        ffmpeg_proc = None
                        await safe_screenshot(page, f"{SCREENSHOT_DIR}/stream_ended_{segment_index:02d}.png")
                        await leave_stream_and_voice(page, guild_url)

                    consecutive_loop_errors = 0
                    await asyncio.sleep(5)

                except Exception as loop_err:
                    consecutive_loop_errors += 1
                    print(f"[ERROR] Main loop exception ({consecutive_loop_errors}): {loop_err}")
                    if ffmpeg_proc:
                        stop_ffmpeg(ffmpeg_proc)
                        ffmpeg_proc = None
                    await leave_stream_and_voice(page, guild_url)

                    if consecutive_loop_errors >= 3:
                        print("[RECOVERY] Reloading Discord guild page...")
                        try:
                            await page.goto(guild_url, wait_until="domcontentloaded", timeout=60000)
                            await asyncio.sleep(8)
                            consecutive_loop_errors = 0
                        except Exception as nav_err:
                            print(f"[ERROR] Reload failed: {nav_err}")
                    await asyncio.sleep(5)

        finally:
            if ffmpeg_proc:
                stop_ffmpeg(ffmpeg_proc)
            await leave_stream_and_voice(page, guild_url)
            await browser.close()
            print("[SHUTDOWN] Browser closed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
