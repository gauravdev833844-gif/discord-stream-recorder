# Discord Floxi Indori Cloud Recorder

Automated 24/7 cloud recorder running on GitHub Actions to capture Floxi Indori's Discord streams and voice channels in 1080p MP4.

## Features:
- Runs headless on GitHub Actions (Ubuntu)
- Real Chromium browser running inside Xvfb (Virtual Display :99)
- PulseAudio virtual sound sink capturing all voice channel participants
- Automatic stream detection and "Watch Stream" trigger
- Saves video to GitHub Actions Artifacts and Gofile

