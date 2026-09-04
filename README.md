# obs-steam-achievements

OBS Python script that automatically adds a chapter marker to your recording whenever you unlock a Steam achievement.

## Requirements

- OBS Studio 30.2+
- Python 3.10+

## Setup

1. In OBS go to **Tools → Scripts 
2. Add `obs_steam_achievements.py` via the **+** button
3. Start recording and play a Steam game

Achievements will appear as named chapter markers (`🏆 Achievement Name`) in your recording timeline.

## Notes

- Steam userdata path is auto-detected on Linux and Windows
- Game detection is automatic — no configuration needed
- Games without achievements are silently ignored
