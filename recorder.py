import os
import sys
import time
import subprocess
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding='utf-8')

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("❌ ERROR: DISCORD_TOKEN environment variable is missing!")
    sys.exit(1)

clean_token = TOKEN.strip().strip('"').strip("'")
GUILD_ID = "1429959255309684798"
TARGET_CHANNELS = [
    ("1429959256651595930", "FLOXI ARMY"),
    ("1529963618580562040", "FLOXI ARMY 4"),
    ("1550210403248771194", "floxi 3"),
    ("1524287475940462694", "mods meeting"),
    ("1553496804211818576", "inner circle"),
    ("1565009474773848124", "ic"),
]

RECORDING_FILE = "Floxi_Indori_Live.mp4"
SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

async def main():
    print("=" * 65)
    print("🚀 DISCORD CLOUD RUNNER RECORDER STARTING")
    print(f"Target Guild: {GUILD_ID} (FLOXI INDORI ARMY)")
    print(f"Monitoring {len(TARGET_CHANNELS)} Voice Channels")
    print("=" * 65)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )

        # Injects token into localStorage on every page navigation
        init_script = f"""
            try {{
                window.localStorage.setItem('token', JSON.stringify("{clean_token}"));
            }} catch(e) {{}}
        """
        await context.add_init_script(init_script)

        page = await context.new_page()

        target_url = f"https://discord.com/channels/{GUILD_ID}/1429959256651595930"
        print(f"[1/3] Navigating to Discord target channel: {target_url}")
        try:
            await page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"Navigation warning: {e}")

        # Wait 10 seconds for Discord React app to initialize
        print("[2/3] Waiting for Discord React application to load...")
        await asyncio.sleep(10)

        # Save verification screenshot
        screenshot_path = os.path.join(SCREENSHOT_DIR, "login_verification.png")
        await page.screenshot(path=screenshot_path)
        print(f"📸 Verification screenshot saved: {screenshot_path}")

        print("\n[3/3] Actively monitoring for Floxi Indori live streams...")
        ffmpeg_proc = None
        is_recording = False
        start_time = time.time()
        max_duration = 3.5 * 3600  # 3.5 hours per shift

        loop_count = 0
        while (time.time() - start_time) < max_duration:
            loop_count += 1
            try:
                # Check target channels
                for channel_id, channel_name in TARGET_CHANNELS:
                    selector = f'a[href*="/channels/{GUILD_ID}/{channel_id}"]'
                    channel_elem = await page.query_selector(selector)
                    if not channel_elem:
                        continue

                    # Check for LIVE badge
                    has_live = await page.evaluate("""(chId) => {
                        const link = document.querySelector(`a[href*="${chId}"]`);
                        if (!link) return false;
                        const container = link.closest('li') || link.parentElement;
                        if (!container) return false;
                        const txt = container.textContent || "";
                        return txt.includes("LIVE") || container.querySelector('[class*="live"]') !== null;
                    }""", channel_id)

                    if has_live and not is_recording:
                        print(f"\n🔴 [STREAM DETECTED] in {channel_name} ({channel_id})! Connecting...")
                        await channel_elem.click()
                        await asyncio.sleep(3)

                        # Click "Watch Stream"
                        clicked_watch = await page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                            const btn = btns.find(b => {
                                const t = (b.textContent || "").toLowerCase();
                                return t.includes("watch stream") || t === "watch" || t.includes("join stream");
                            });
                            if (btn) {
                                btn.click();
                                return true;
                            }
                            const tile = document.querySelector('[class*="streamPreview"], [class*="videoWrapper"]');
                            if (tile) {
                                tile.click();
                                return true;
                            }
                            return false;
                        }""")

                        if clicked_watch:
                            print("✅ Clicked 'Watch Stream'!")
                        await asyncio.sleep(2)

                        # Stream started screenshot
                        await page.screenshot(path=os.path.join(SCREENSHOT_DIR, "stream_started.png"))

                        # Start FFmpeg recording
                        print(f"📹 Spawning FFmpeg 1080p recorder -> {RECORDING_FILE}")
                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-video_size", "1920x1080",
                            "-framerate", "30",
                            "-f", "x11grab",
                            "-i", ":99.0",
                            "-f", "pulse",
                            "-i", "DiscordSink.monitor",
                            "-c:v", "libx264",
                            "-preset", "veryfast",
                            "-pix_fmt", "yuv420p",
                            "-c:a", "aac",
                            "-b:a", "192k",
                            RECORDING_FILE
                        ]
                        ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                        is_recording = True
                        break

                # If recording, monitor if stream ended
                if is_recording:
                    stream_ended = await page.evaluate("""() => {
                        const txt = document.body.textContent || "";
                        const video = document.querySelector('video');
                        return txt.includes("Stream has ended") || txt.includes("is not streaming") || !video;
                    }""")

                    if stream_ended:
                        print("\n⏹️ Stream ended. Stopping FFmpeg cleanly...")
                        try:
                            ffmpeg_proc.stdin.write(b'q')
                            ffmpeg_proc.stdin.flush()
                            ffmpeg_proc.wait(timeout=15)
                        except Exception as e:
                            print("Force killing FFmpeg:", e)
                            ffmpeg_proc.kill()
                        
                        is_recording = False
                        await page.screenshot(path=os.path.join(SCREENSHOT_DIR, "stream_ended.png"))
                        print("🎉 Recording saved successfully!")
                        break

                if loop_count % 60 == 0:
                    print(f"⏱️ Monitoring active... (Elapsed: {int((time.time() - start_time) / 60)} mins)")

            except Exception as e:
                print(f"Monitoring loop warning: {e}")

            await asyncio.sleep(5)

        # Cleanup at shift end
        if is_recording and ffmpeg_proc:
            print("Shift ended. Finalizing recording...")
            try:
                ffmpeg_proc.stdin.write(b'q')
                ffmpeg_proc.stdin.flush()
                ffmpeg_proc.wait(timeout=15)
            except Exception:
                ffmpeg_proc.kill()

        await browser.close()
        print("Recorder task finished.")

if __name__ == "__main__":
    asyncio.run(main())
