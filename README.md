# 💧 Hydration & Activity Monitor

A desktop application that monitors your keyboard/mouse activity and reminds you to drink water at optimal intervals.

## Features

- 💧 **Smart Water Reminders** - Popup notifications to stay hydrated
- ⌨️ **Activity Tracking** - Monitors keyboard and mouse activity
- 📊 **Dashboard** - Visual analytics with charts and insights
- 🔔 **Activity Alerts** - Detects inactivity, hyperactivity, and low activity patterns
- 📈 **Statistics** - Tracks water consumption and activity patterns over time

## Screenshots

The dashboard includes:
- Water consumption tracking (drank vs skipped)
- Activity pattern analysis (inactive, hyperactive, low activity)
- Timeline visualizations
- Personalized insights

## Requirements

```
python 3.8+
tkinter
matplotlib
pynput
plyer
```

## Installation

```bash
pip install matplotlib pynput plyer
```

## Usage

```bash
python Popout.py
```

The app will:
1. Open a dashboard window
2. Start monitoring your activity
3. Send water reminders at regular intervals
4. Track your responses and activity patterns

## How It Works

1. **Activity Detection**: Uses `pynput` to track keyboard and mouse events
2. **Pattern Analysis**: Calculates typing speed and detects activity states
3. **Smart Reminders**: Adjusts water reminder intervals based on your activity
4. **Data Persistence**: Saves stats to `user_data.json` for tracking over time

## Tech Stack

- Python
- Tkinter (GUI)
- Matplotlib (Charts)
- Pynput (Input monitoring)
- Plyer (Desktop notifications)

## License

MIT License
