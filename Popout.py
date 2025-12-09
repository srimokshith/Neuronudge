# notifTest_fixed.py
# Fixed + hardened version of your Hydration + Activity monitor.
# Major fixes:
#  - Force matplotlib to use the TkAgg backend BEFORE importing pyplot (prevents white/blank canvas).
#  - Detect and degrade gracefully when pynput cannot run (common on headless / permission-limited systems).
#  - Improved debug logging so you can see whether listeners started and why notifications might not fire.
#  - Ensure all FigureCanvasTkAgg.draw() calls happen after widgets are packed and Tk root exists.
#  - Use main-thread UI creation only (popups are queued and created in tk_pump).
#  - Minor logic hardening for activity detection.
#
# NOTE: If you run on Linux/X, run normally. If keyboard/mouse monitoring doesn't start, check:
#   - Are you running under Wayland? pynput may need Xorg or extra permissions.
#   - Try running with sudo (only as a quick test) or enable X accessibility options.
# The script falls back to a "simulated activity" mode when pynput fails.

import threading
import time
import json
import os
import platform
import queue
import warnings
import tkinter as tk
from tkinter import ttk, messagebox
import sys
import signal
from datetime import datetime

# ---- IMPORTANT: set matplotlib backend before importing pyplot / FigureCanvasTkAgg ----
import matplotlib
# Force the TkAgg backend used by Tkinter embeddings
try:
    matplotlib.use("TkAgg")
except Exception:
    # If this fails, we'll still try to continue; canvas may not show correctly
    print("⚠️ Couldn't set matplotlib TkAgg backend; fallback to default backend.")

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates

# ========== INITIAL SETUP ==========
if platform.system() == "Linux":
    # Filter dbus warnings (harmless)
    warnings.filterwarnings("ignore", message=".*dbus.*")

ROOT_DIR = os.path.dirname(__file__)
DATA_FILE = os.path.join(ROOT_DIR, "user_data.json")

# Thread-safe globals
lock = threading.Lock()
root_window = None
dashboard_window = None
popup_queue = queue.Queue()
dashboard_queue = queue.Queue()

# Activity trackers
last_key_time = 0.0  # 0 => no key seen yet
last_mouse_time = time.perf_counter()
key_intervals = []  # store (timestamp, interval)
activity_window_seconds = 10  # short window for testing

# Hydration tracker
last_water_reminder = time.perf_counter()

# Stats
stats = {
    "water_drunk_count": 0,
    "water_skipped_count": 0,
    "inactive_duration": 0.0,
    "hyperactive_duration": 0.0,
    "lowactive_duration": 0.0,
    "last_state": "neutral",
    "water_reminder_times": [],
    "water_response_times": [],
    "optimal_reminder_interval": 10,  # seconds (testing)
}

# ========== DATA HANDLING ==========
def load_data():
    global stats
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                loaded = json.load(f)
                # Only update known keys to avoid unexpected data
                for k in stats.keys():
                    if k in loaded:
                        stats[k] = loaded[k]
        except Exception as e:
            print(f"⚠️ Failed to load data file: {e}")
    # Ensure test-friendly interval
    if not (10 <= stats.get("optimal_reminder_interval", 10) <= 300):
        stats["optimal_reminder_interval"] = 10
        save_data()

def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save data: {e}")

load_data()

# ========== NOTIFICATIONS ==========
def notify(title, message):
    """Try desktop notification, fallback to simple popup (queued), fallback to console."""
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=5)
        return
    except Exception:
        # plyer not available or failed - fallback to Tk popup (queued to main thread)
        print(f"[NOTIFY FALLBACK] {title}: {message}")
        # We do not create popup here from background thread; queue it for main thread
        try:
            popup_queue.put(("text", title, message))
        except Exception:
            pass

# ========== WATER POPUP (created only in main/UI thread) ==========
def show_water_popup():
    global root_window
    if root_window is None:
        print("💧 ERROR: root_window is None, cannot show water popup")
        return

    popup = tk.Toplevel(root_window)
    popup.title("💧 Hydration Reminder")
    popup.geometry("380x200")
    popup.configure(bg="#e3f2fd")
    popup.attributes("-topmost", True)
    popup.resizable(False, False)

    # content
    tk.Label(popup, text="💧", font=("Arial", 48), bg="#e3f2fd").pack(pady=(10, 0))
    tk.Label(popup, text="Time to drink water!", font=("Arial", 14, "bold"), bg="#e3f2fd", fg="#1976d2").pack(pady=(5, 8))

    frame = ttk.Frame(popup)
    frame.pack(pady=5)

    def on_yes():
        with lock:
            stats["water_drunk_count"] += 1
            stats["water_response_times"].append({"time": time.time(), "drank": True})
            save_data()
        try:
            dashboard_queue.put("update")
        except Exception:
            pass
        popup.destroy()

    def on_no():
        with lock:
            stats["water_skipped_count"] += 1
            stats["water_response_times"].append({"time": time.time(), "drank": False})
            save_data()
        try:
            dashboard_queue.put("update")
        except Exception:
            pass
        popup.destroy()

    ttk.Button(frame, text="Yes, I drank", command=on_yes).pack(side=tk.LEFT, padx=10)
    ttk.Button(frame, text="No", command=on_no).pack(side=tk.LEFT, padx=10)

    # auto-close and treat as "No" after timeout
    popup.after(30000, on_no)

    # ensure displayed properly
    popup.update_idletasks()
    popup.lift()
    popup.update()

# ========== DASHBOARD UI ==========
# We'll keep references to widgets so update_dashboard can alter them
water_drunk_label = water_skipped_label = water_rate_label = None
inactive_label = hyperactive_label = lowactive_label = current_state_label = None
insights_text_widget = None

overview_fig = water_fig = activity_fig = None
overview_canvas = water_canvas = activity_canvas = None

def create_dashboard():
    global dashboard_window, overview_fig, overview_canvas, water_fig, water_canvas, activity_fig, activity_canvas
    if dashboard_window is not None:
        try:
            dashboard_window.deiconify()
            dashboard_window.lift()
            dashboard_window.attributes("-topmost", True)
            dashboard_window.after(1000, lambda: dashboard_window.attributes("-topmost", False))
        except Exception:
            pass
        return

    dashboard_window = tk.Toplevel(root_window)
    dashboard_window.title("Activity & Hydration Dashboard")
    dashboard_window.geometry("1200x800")
    dashboard_window.configure(bg="#f5f5f5")

    dashboard_frame = ttk.Frame(dashboard_window, padding="10")
    dashboard_frame.pack(fill=tk.BOTH, expand=True)

    notebook = ttk.Notebook(dashboard_frame)
    notebook.pack(fill=tk.BOTH, expand=True)

    overview_tab = ttk.Frame(notebook); notebook.add(overview_tab, text="Overview")
    water_tab = ttk.Frame(notebook); notebook.add(water_tab, text="Water Consumption")
    activity_tab = ttk.Frame(notebook); notebook.add(activity_tab, text="Activity Analysis")
    insights_tab = ttk.Frame(notebook); notebook.add(insights_tab, text="Insights")

    create_overview_tab(overview_tab)
    create_water_tab(water_tab)
    create_activity_tab(activity_tab)
    create_insights_tab(insights_tab)

    btn_frame = ttk.Frame(dashboard_frame)
    btn_frame.pack(fill=tk.X, pady=8)
    refresh_button = ttk.Button(btn_frame, text="Refresh Data", command=lambda: dashboard_queue.put("update"))
    refresh_button.pack(side=tk.LEFT, padx=6)
    close_button = ttk.Button(btn_frame, text="Close Dashboard", command=close_dashboard)
    close_button.pack(side=tk.LEFT, padx=6)

    dashboard_window.protocol("WM_DELETE_WINDOW", close_dashboard)

    # Give a small delay then update (ensures canvases are realized)
    dashboard_window.after(200, update_dashboard)

def create_overview_tab(tab):
    global water_drunk_label, water_skipped_label, water_rate_label
    global inactive_label, hyperactive_label, lowactive_label, current_state_label
    global overview_fig, overview_canvas

    frame = ttk.Frame(tab, padding="10"); frame.pack(fill=tk.BOTH, expand=True)
    title_label = ttk.Label(frame, text="Activity & Hydration Overview", font=("Arial", 16, "bold"))
    title_label.pack(pady=6)

    stats_frame = ttk.Frame(frame); stats_frame.pack(fill=tk.X)

    water_frame = ttk.LabelFrame(stats_frame, text="Water Consumption", padding="10")
    water_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

    water_drunk_label = ttk.Label(water_frame, text="Water Drank: 0", font=("Arial", 12)); water_drunk_label.pack(anchor=tk.W)
    water_skipped_label = ttk.Label(water_frame, text="Water Skipped: 0", font=("Arial", 12)); water_skipped_label.pack(anchor=tk.W)
    water_rate_label = ttk.Label(water_frame, text="Response Rate: 0%", font=("Arial", 12)); water_rate_label.pack(anchor=tk.W)

    activity_frame = ttk.LabelFrame(stats_frame, text="Activity Patterns", padding="10")
    activity_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

    inactive_label = ttk.Label(activity_frame, text="Inactive Time: 0s", font=("Arial", 12)); inactive_label.pack(anchor=tk.W)
    hyperactive_label = ttk.Label(activity_frame, text="Hyperactive Time: 0s", font=("Arial", 12)); hyperactive_label.pack(anchor=tk.W)
    lowactive_label = ttk.Label(activity_frame, text="Low Activity Time: 0s", font=("Arial", 12)); lowactive_label.pack(anchor=tk.W)
    current_state_label = ttk.Label(activity_frame, text="Current State: Neutral", font=("Arial", 12)); current_state_label.pack(anchor=tk.W)

    # Pie chart
    overview_fig = Figure(figsize=(6, 4), dpi=100)
    overview_canvas = FigureCanvasTkAgg(overview_fig, master=frame)
    overview_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, pady=10)
    overview_canvas.draw()

def create_water_tab(tab):
    global water_fig, water_canvas
    frame = ttk.Frame(tab, padding="10"); frame.pack(fill=tk.BOTH, expand=True)
    title_label = ttk.Label(frame, text="Water Consumption Analysis", font=("Arial", 16, "bold"))
    title_label.pack(pady=6)

    water_fig = Figure(figsize=(10, 6), dpi=100)
    water_canvas = FigureCanvasTkAgg(water_fig, master=frame)
    water_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
    # draw after packing
    water_canvas.draw()

def create_activity_tab(tab):
    global activity_fig, activity_canvas
    frame = ttk.Frame(tab, padding="10"); frame.pack(fill=tk.BOTH, expand=True)
    title_label = ttk.Label(frame, text="Activity Pattern Analysis", font=("Arial", 16, "bold"))
    title_label.pack(pady=6)

    activity_fig = Figure(figsize=(10, 8), dpi=100)
    activity_canvas = FigureCanvasTkAgg(activity_fig, master=frame)
    activity_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
    activity_canvas.draw()

def create_insights_tab(tab):
    global insights_text_widget
    frame = ttk.Frame(tab, padding="10"); frame.pack(fill=tk.BOTH, expand=True)
    title_label = ttk.Label(frame, text="Activity & Hydration Insights", font=("Arial", 16, "bold"))
    title_label.pack(pady=6)

    insights_text_widget = tk.Text(frame, wrap=tk.WORD, height=20)
    insights_text_widget.pack(fill=tk.BOTH, expand=True)
    # scrollbar
    scrollbar = ttk.Scrollbar(frame, command=insights_text_widget.yview)
    insights_text_widget.config(yscrollcommand=scrollbar.set)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

def update_dashboard():
    """Update all labels and redraw charts. Safe to call from main thread only."""
    global overview_fig, overview_canvas, water_fig, water_canvas, activity_fig, activity_canvas
    if dashboard_window is None:
        return

    with lock:
        water_drunk = stats.get("water_drunk_count", 0)
        water_skipped = stats.get("water_skipped_count", 0)
        total_water_responses = water_drunk + water_skipped
        water_rate = (water_drunk / total_water_responses * 100) if total_water_responses > 0 else 0.0

        inactive_duration = stats.get("inactive_duration", 0.0)
        hyperactive_duration = stats.get("hyperactive_duration", 0.0)
        lowactive_duration = stats.get("lowactive_duration", 0.0)
        last_state = stats.get("last_state", "neutral")

    # Update labels
    try:
        water_drunk_label.config(text=f"Water Drank: {water_drunk}")
        water_skipped_label.config(text=f"Water Skipped: {water_skipped}")
        water_rate_label.config(text=f"Response Rate: {water_rate:.1f}%")

        inactive_label.config(text=f"Inactive Time: {inactive_duration:.1f}s")
        hyperactive_label.config(text=f"Hyperactive Time: {hyperactive_duration:.1f}s")
        lowactive_label.config(text=f"Low Activity Time: {lowactive_duration:.1f}s")
        current_state_label.config(text=f"Current State: {last_state.capitalize()}")
    except Exception:
        pass

    # Overview pie
    try:
        overview_fig.clear()
        ax = overview_fig.add_subplot(111)
        activity_data = [inactive_duration, hyperactive_duration, lowactive_duration]
        activity_labels = ['Inactive', 'Hyperactive', 'Low Activity']
        if sum(activity_data) > 0:
            ax.pie(activity_data, labels=activity_labels, autopct='%1.1f%%', startangle=90)
            ax.axis('equal')
        else:
            ax.text(0.5, 0.5, 'No activity data yet', horizontalalignment='center', verticalalignment='center')
        overview_canvas.draw()
    except Exception as e:
        print("⚠️ Error drawing overview:", e)

    # Water plot
    try:
        water_fig.clear()
        ax = water_fig.add_subplot(111)
        with lock:
            water_response_times = list(stats.get("water_response_times", []))
        drank_times = [r["time"] for r in water_response_times if r.get("drank")]
        skipped_times = [r["time"] for r in water_response_times if not r.get("drank")]

        if drank_times or skipped_times:
            drank_datetimes = [datetime.fromtimestamp(t) for t in drank_times]
            skipped_datetimes = [datetime.fromtimestamp(t) for t in skipped_times]
            if drank_datetimes:
                drank_datetimes.sort()
                drank_counts = list(range(1, len(drank_datetimes) + 1))
                ax.plot(drank_datetimes, drank_counts, marker='o', label='Drank')
            if skipped_datetimes:
                skipped_datetimes.sort()
                skipped_counts = list(range(1, len(skipped_datetimes) + 1))
                ax.plot(skipped_datetimes, skipped_counts, marker='x', label='Skipped')
            ax.set_title("Water Consumption Over Time")
            ax.set_xlabel("Time")
            ax.set_ylabel("Cumulative Count")
            ax.grid(True)
            ax.legend()
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            water_fig.autofmt_xdate()
        else:
            ax.text(0.5, 0.5, 'No water consumption data yet', horizontalalignment='center', verticalalignment='center')

        water_canvas.draw()
    except Exception as e:
        print("⚠️ Error drawing water figure:", e)

    # Activity plot
    try:
        activity_fig.clear()
        ax1 = activity_fig.add_subplot(211)
        with lock:
            reminder_times = list(stats.get("water_reminder_times", []))
        if reminder_times:
            reminder_datetimes = [datetime.fromtimestamp(t) for t in reminder_times]
            ax1.plot(reminder_datetimes, [1]*len(reminder_datetimes), 'go')
            ax1.set_title("Activity Timeline (reminders shown)")
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            activity_fig.autofmt_xdate()
        else:
            ax1.text(0.5, 0.5, 'No activity timeline data yet', horizontalalignment='center', verticalalignment='center')

        ax2 = activity_fig.add_subplot(212)
        activity_data = [inactive_duration, hyperactive_duration, lowactive_duration]
        labels = ['Inactive', 'Hyperactive', 'Low Activity']
        if sum(activity_data) > 0:
            bars = ax2.bar(labels, activity_data)
            for bar, v in zip(bars, activity_data):
                ax2.text(bar.get_x() + bar.get_width()/2., v, f'{v:.1f}s', ha='center', va='bottom')
        else:
            ax2.text(0.5, 0.5, 'No activity distribution data yet', horizontalalignment='center', verticalalignment='center')

        activity_fig.tight_layout()
        activity_canvas.draw()
    except Exception as e:
        print("⚠️ Error drawing activity figure:", e)

    # Insights text
    try:
        insights_text_widget.delete(1.0, tk.END)
        insights_text_widget.insert(tk.END, generate_insights())
    except Exception:
        pass

def generate_insights():
    with lock:
        water_drunk = stats.get("water_drunk_count", 0)
        water_skipped = stats.get("water_skipped_count", 0)
        total = water_drunk + water_skipped
        water_rate = (water_drunk / total * 100) if total > 0 else 0.0
        inactive_duration = stats.get("inactive_duration", 0)
        hyperactive_duration = stats.get("hyperactive_duration", 0)
        lowactive_duration = stats.get("lowactive_duration", 0)
        optimal_interval = stats.get("optimal_reminder_interval", 10)
        last_state = stats.get("last_state", "neutral")

        s = []
        s.append("ACTIVITY & HYDRATION INSIGHTS")
        s.append("="*50)
        s.append("")
        s.append(f"WATER: drank {water_drunk} | skipped {water_skipped} | response rate {water_rate:.1f}%")
        s.append(f"Current water reminder interval: {optimal_interval} seconds")
        s.append("")
        s.append("ACTIVITY PATTERNS:")
        tot = inactive_duration + hyperactive_duration + lowactive_duration
        if tot > 0:
            s.append(f"- Inactive: {inactive_duration:.1f}s")
            s.append(f"- Hyperactive: {hyperactive_duration:.1f}s")
            s.append(f"- Low activity: {lowactive_duration:.1f}s")
        else:
            s.append("- Not enough activity data yet.")
        s.append("")
        s.append(f"Current state: {last_state}")
        return "\n".join(s)

def close_dashboard():
    global dashboard_window
    if dashboard_window:
        try:
            dashboard_window.destroy()
        finally:
            dashboard_window = None

# ========== HYDRATION REMINDER ==========
def recommend_interval():
    # Keep simple for testing
    return stats.get("optimal_reminder_interval", 10)

def water_reminder_thread():
    global last_water_reminder
    print("💧 Water reminder system active.")
    while True:
        try:
            interval = recommend_interval()
            elapsed = time.perf_counter() - last_water_reminder
            if elapsed >= interval:
                print(f"💧 Triggering water reminder (interval {interval}s)")
                with lock:
                    stats["water_reminder_times"].append(time.time())
                    save_data()
                popup_queue.put("water")
                dashboard_queue.put("update")
                last_water_reminder = time.perf_counter()
        except Exception as e:
            print("⚠️ Error in water_reminder_thread:", e)
        time.sleep(1)

# ========== ACTIVITY DETECTION ==========
def record_state(state):
    with lock:
        stats["last_state"] = state
        save_data()

def activity_monitor_thread():
    print("🏃 Activity monitor started.")
    last_notification_time = {"inactive": 0, "low": 0, "high": 0}
    inactivity_threshold = 10  # seconds for testing
    while True:
        now = time.perf_counter()
        with lock:
            # compute times
            time_since_key = now - last_key_time if last_key_time > 0 else float('inf')
            time_since_mouse = now - last_mouse_time if last_mouse_time > 0 else float('inf')
            cutoff_time = now - activity_window_seconds
            recent_intervals = [iv for ts, iv in key_intervals if ts > cutoff_time]

        # Debug prints so user can see activity analysis progress
        if last_key_time == 0:
            print("🏃 Waiting for first key press to start activity pattern detection...")
        else:
            print(f"🏃 Typing recent_intervals={len(recent_intervals)} last_key_ago={time_since_key:.1f}s")

        # INACTIVITY: no keyboard and no mouse
        if last_key_time > 0 and time_since_key > inactivity_threshold and time_since_mouse > inactivity_threshold:
            if now - last_notification_time["inactive"] > 10:
                record_state("inactive")
                with lock:
                    stats["inactive_duration"] += inactivity_threshold
                    save_data()
                notify("Inactivity Detected", f"No keyboard/mouse activity for {inactivity_threshold} seconds.")
                last_notification_time["inactive"] = now
                dashboard_queue.put("update")

        # Pattern detection if enough recent data
        elif last_key_time > 0 and len(recent_intervals) >= 3:
            avg_interval = sum(recent_intervals) / len(recent_intervals)
            if avg_interval < 0.2 and now - last_notification_time["high"] > 10 and len(recent_intervals) >= 5:
                record_state("hyperactive")
                with lock:
                    stats["hyperactive_duration"] += 5
                    save_data()
                notify("High Activity", f"Rapid typing (avg {avg_interval:.3f}s). Take a break.")
                last_notification_time["high"] = now
                dashboard_queue.put("update")
            elif avg_interval > 0.7 and now - last_notification_time["low"] > 10:
                record_state("lowactive")
                with lock:
                    stats["lowactive_duration"] += 5
                    save_data()
                notify("Low Activity", f"Slow typing detected (avg {avg_interval:.3f}s). Stay focused.")
                last_notification_time["low"] = now
                dashboard_queue.put("update")
        else:
            # not enough data yet
            pass

        time.sleep(1)

# ========== EVENT LISTENERS / FALLBACK ==========
simulate_activity = False

def on_press(key):
    global last_key_time
    now = time.perf_counter()
    with lock:
        if last_key_time != 0:
            interval = now - last_key_time
            if 0 < interval < 2:
                key_intervals.append((now, interval))
                cutoff = now - activity_window_seconds
                key_intervals[:] = [(t,i) for t,i in key_intervals if t > cutoff]
                if len(key_intervals) % 10 == 0:
                    print(f"⌨️ Keyboard: tracked {len(key_intervals)} intervals")
        last_key_time = now

def on_move(x, y):
    global last_mouse_time
    last_mouse_time = time.perf_counter()

def keyboard_listener():
    global simulate_activity
    try:
        from pynput import keyboard as kb
        print("⌨️ Keyboard listener starting...")
        with kb.Listener(on_press=on_press) as listener:
            listener.join()
    except Exception as e:
        print(f"⌨️ Keyboard listener failed: {e}")
        print("⌨️ Falling back to simulated activity mode.")
        simulate_activity = True

def mouse_listener():
    global simulate_activity
    try:
        from pynput import mouse as ms
        print("🖱️ Mouse listener starting...")
        with ms.Listener(on_move=on_move) as listener:
            listener.join()
    except Exception as e:
        print(f"🖱️ Mouse listener failed: {e}")
        simulate_activity = True

# A small thread to simulate activity when listeners can't run (useful for testing)
def simulated_activity_thread():
    print("⚠️ Running simulated activity thread (pynput unavailable).")
    while True:
        # push a fake key press event every 3 seconds so monitoring logic can run
        now = time.perf_counter()
        with lock:
            if last_key_time == 0:
                # initialize so activity monitor doesn't think user never typed
                pass
            # create a fake interval: alternate small and medium intervals
            key_intervals.append((now, 0.3))
            cutoff = now - activity_window_seconds
            key_intervals[:] = [(t,i) for t,i in key_intervals if t > cutoff]
            # pretend user typed now
            globals()['last_key_time'] = now
        time.sleep(3)

# ========== COMMAND LISTENER (terminal) ==========
def command_listener():
    print("📊 Type 'dashboard' in the terminal to open dashboard (or press the GUI buttons).")
    buffer = ""
    while True:
        try:
            import select
            if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
                char = sys.stdin.read(1)
                buffer += char
                if 'dashboard' in buffer.lower():
                    dashboard_queue.put("open")
                    buffer = ""
                if len(buffer) > 50:
                    buffer = buffer[-50:]
        except Exception:
            time.sleep(1)

# ========== TK PUMP ==========
def tk_pump():
    try:
        # process popup queue
        while True:
            try:
                cmd = popup_queue.get_nowait()
            except queue.Empty:
                break

            # support two types: legacy "water" string and tuple types
            if cmd == "water":
                show_water_popup()
            elif isinstance(cmd, tuple):
                typ = cmd[0]
                if typ == "text":
                    _, title, text = cmd
                    # show a small messagebox
                    try:
                        messagebox.showinfo(title, text)
                    except Exception:
                        print(f"[POPUP] {title}: {text}")

        # dashboard commands
        try:
            cmd = dashboard_queue.get_nowait()
            if cmd == "open":
                create_dashboard()
            elif cmd == "update":
                update_dashboard()
        except queue.Empty:
            pass

        # Always refresh dashboard (lightweight) so UI doesn't stay blank
        update_dashboard()
    except Exception as e:
        print("⚠️ Error in tk_pump:", e)
    finally:
        if root_window and root_window.winfo_exists():
            root_window.after(500, tk_pump)

# ========== MAIN ==========
print("🚀 Starting Hydration + Activity Monitor (fixed).")

# Create Tk root on main thread
root_window = tk.Tk()
root_window.withdraw()  # hide main window
# create dashboard immediately (will be shown)
create_dashboard()

# Start threads
threads = []
t = threading.Thread(target=water_reminder_thread, daemon=True); threads.append(t)
t = threading.Thread(target=activity_monitor_thread, daemon=True); threads.append(t)
t = threading.Thread(target=keyboard_listener, daemon=True); threads.append(t)
t = threading.Thread(target=mouse_listener, daemon=True); threads.append(t)
t = threading.Thread(target=command_listener, daemon=True); threads.append(t)

for th in threads:
    th.start()
    time.sleep(0.15)

# If both keyboard & mouse listeners failed, start simulation
# Check simulate_activity after giving listeners a short moment to fail
time.sleep(0.5)
if 'simulate_activity' in globals() and globals().get('simulate_activity', False):
    sim = threading.Thread(target=simulated_activity_thread, daemon=True)
    sim.start()

print("✅ Threads started. Monitoring active. Opened dashboard (should display charts).")

# start UI pump and mainloop
root_window.after(200, tk_pump)

def signal_handler(sig, frame):
    print("Shutting down...")
    save_data()
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
try:
    root_window.mainloop()
except KeyboardInterrupt:
    save_data()
    os._exit(0)

