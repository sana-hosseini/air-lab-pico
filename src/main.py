"""
Air Lab Pico Edition - Main Application
MicroPython v1.26.1 (2025-09-11) for Raspberry Pi Pico 2 (RP2350)

Production-ready air quality monitoring system featuring:
- Two-tier storage: Fast RAM circular buffers + persistent binary flash logging
- 7-page OLED display: Dashboard, 5 individual sensor graphs, system diagnostics
- Smart memory management: Pre-allocated arrays, conservative GC
- Robust operation: Watchdog timer, automatic error recovery, OLED burn-in protection
"""

import gc
import time
import math

# === USER CONFIGURATION ===
DATA_SAMPLE_INTERVAL = 30    # Seconds between samples (10, 30, 60, 300, 600)
SCREEN_TIMEOUT = 60          # Screen blank timeout (seconds)
DISPLAY_REFRESH_MS = 1000    # Display update interval (1000 = 1Hz)
BUTTON_DEBOUNCE_MS = 50      # Button debounce time
WATCHDOG_TIMEOUT_MS = 16000  # Watchdog timer (16 seconds)
RAM_USAGE_TARGET = 0.60      # Use 60% of available RAM for buffers

# === MEMORY ALLOCATION (CRITICAL - MUST BE BEFORE HARDWARE IMPORTS) ===

# Force GC before allocation
gc.collect()

# Conservative overhead calculation
BYTES_PER_SAMPLE = 40  # 5 metrics × 8 bytes (4 float + 4 timestamp)
FREE_RAM = gc.mem_free()
OVERHEAD_KB = 80  # Based on real-world testing
MODULE_COMPILE_KB = 30  # Budget for runtime compilation

AVAILABLE_RAM = FREE_RAM - ((OVERHEAD_KB + MODULE_COMPILE_KB) * 1024)
BUFFER_CAPACITY = int((AVAILABLE_RAM * RAM_USAGE_TARGET) / BYTES_PER_SAMPLE)

# Validate minimum capacity
if BUFFER_CAPACITY < 1000:
    raise ValueError(f"Insufficient RAM: need 1000+ samples, got {BUFFER_CAPACITY}")

print("=" * 40)
print("AIR LAB PICO EDITION - v3.0")
print("=" * 40)
print(f"Total RAM: {FREE_RAM // 1024}KB")
print(f"Reserved: {OVERHEAD_KB + MODULE_COMPILE_KB}KB")
print(f"Available: {AVAILABLE_RAM // 1024}KB")
print(f"Allocating {RAM_USAGE_TARGET * 100:.0f}%: {BUFFER_CAPACITY} samples")
print(f"  = {(BUFFER_CAPACITY * DATA_SAMPLE_INTERVAL) / 3600:.1f} hours")

# Import and allocate DataLogger FIRST (big arrays)
from datalogger import DataLogger
logger = DataLogger(BUFFER_CAPACITY, DATA_SAMPLE_INTERVAL)

print(f"Buffers allocated. Free RAM: {gc.mem_free() // 1024}KB")

# NOW import hardware drivers
from machine import I2C, Pin, SPI, WDT
from ssd1309 import SSD1309_SPI
from aht21 import AHT21
from ens160 import ENS160

# === PIN DEFINITIONS ===
I2C_SDA, I2C_SCL, I2C_FREQ = 4, 5, 400_000
SPI_SCK, SPI_MOSI, SPI_DC, SPI_RST, SPI_CS = 18, 19, 16, 20, 17
BUTTON_PIN, LED_PIN = 15, 25

# === TOTAL PAGES ===
TOTAL_PAGES = 7


def check_fragmentation():
    """
    Non-invasive fragmentation estimate.
    Reports usage percentage instead of allocating test buffers.
    """
    gc.collect()
    free = gc.mem_free()
    alloc = gc.mem_alloc()
    total = free + alloc
    usage_pct = (alloc / total) * 100
    return usage_pct


def initialize_system():
    """Initialize all hardware with error handling."""
    led = Pin(LED_PIN, Pin.OUT)
    led.on()
    
    try:
        # I2C Bus
        print("\nInitializing I2C...")
        i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=I2C_FREQ)
        devices = i2c.scan()
        print(f"I2C devices: {[hex(d) for d in devices]}")
        
        # Sensors
        print("Initializing AHT21...")
        aht = AHT21(i2c)
        
        print("Initializing ENS160...")
        ens = ENS160(i2c)
        
        # SPI Display
        print("Initializing SSD1309...")
        spi = SPI(0, baudrate=10_000_000, sck=Pin(SPI_SCK), mosi=Pin(SPI_MOSI), miso=None)
        oled = SSD1309_SPI(128, 64, spi, dc=Pin(SPI_DC),
                          rst=Pin(SPI_RST), cs=Pin(SPI_CS))
        
        # Splash screen
        stats = logger.get_stats()
        oled.fill(0)
        oled.text("Air Lab Pico", 20, 5, 1)
        oled.text("Edition v3.0", 20, 15, 1)
        oled.text("-" * 16, 0, 25, 1)
        oled.text(f"RAM:{stats['ram_days']:.1f}d", 0, 35, 1)
        oled.text(f"Flash:{stats['flash_mb']:.1f}MB", 0, 45, 1)
        oled.text("18B/sample", 0, 55, 1)
        oled.show()
        time.sleep(3)
        
        # Button
        button = Pin(BUTTON_PIN, Pin.IN, Pin.PULL_UP)
        
        # Watchdog (16s timeout)
        print("Initializing watchdog (16s)...")
        wdt = WDT(timeout=WATCHDOG_TIMEOUT_MS)
        
        # Optimize GC
        gc.collect()
        gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
        
        led.off()
        print(f"\nInit complete. Free RAM: {gc.mem_free() // 1024}KB")
        print("=" * 40)
        
        return {
            'i2c': i2c, 'spi': spi, 'aht': aht, 'ens': ens,
            'oled': oled, 'button': button, 'led': led, 'wdt': wdt
        }
        
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        try:
            oled.fill(0)
            oled.text("FATAL ERROR:", 0, 0, 1)
            oled.text(str(e)[:21], 0, 15, 1)
            oled.text("Check wiring", 0, 35, 1)
            oled.show()
        except:
            pass
        
        # 5Hz strobe = fatal error
        while True:
            led.toggle()
            time.sleep_ms(100)


# === DISPLAY PAGES ===

def draw_page_1_dashboard(oled, temp, hum, ens, data_valid):
    """Page 1: Live readings dashboard."""
    oled.fill(0)
    oled.text("AIR LAB    [1/7]", 0, 0, 1)
    oled.text("-" * 21, 0, 10, 1)
    
    oled.text(f"Temp: {temp:.1f}C", 0, 20, 1)
    oled.text(f"Hum:  {hum:.1f}%", 0, 30, 1)
    
    if not data_valid or ens.warming_up:
        oled.text("AQI:  WARMUP", 0, 40, 1)
        oled.text("eCO2: ---", 0, 50, 1)
    else:
        aqi_labels = ["Exc", "Good", "Mod", "Poor", "Bad"]
        label = aqi_labels[ens.aqi - 1] if 1 <= ens.aqi <= 5 else "ERR"
        oled.text(f"AQI:  {ens.aqi} ({label})", 0, 40, 1)
        oled.text(f"eCO2: {ens.eco2}ppm", 0, 50, 1)
    
    oled.show()


def draw_page_2_temp_graph(oled):
    """Page 2: Temperature graph."""
    oled.fill(0)
    oled.text("Temp (C)   [2/7]", 0, 0, 1)
    
    data = logger.buffers['temp'].get_latest(128)
    
    if len(data) < 2:
        oled.text("No data yet", 20, 30, 1)
        oled.show()
        return
    
    values = [v for t, v in data]
    min_val = min(values)
    max_val = max(values)
    range_val = max(max_val - min_val, 2.0)  # Min 2°C range
    
    graph_height = 51
    for i in range(len(data) - 1):
        y1 = 63 - int(((values[i] - min_val) / range_val) * graph_height)
        y2 = 63 - int(((values[i + 1] - min_val) / range_val) * graph_height)
        oled.line(i, y1, i + 1, y2, 1)
    
    oled.text(f"{max_val:.1f}", 100, 12, 1)
    oled.text(f"{min_val:.1f}", 100, 56, 1)
    oled.show()


def draw_page_3_hum_graph(oled):
    """Page 3: Humidity graph."""
    oled.fill(0)
    oled.text("Humidity % [3/7]", 0, 0, 1)
    
    data = logger.buffers['hum'].get_latest(128)
    
    if len(data) < 2:
        oled.text("No data yet", 20, 30, 1)
        oled.show()
        return
    
    values = [v for t, v in data]
    min_val = min(values)
    max_val = max(values)
    range_val = max(max_val - min_val, 2.0)  # Min 2% range
    
    graph_height = 51
    for i in range(len(data) - 1):
        y1 = 63 - int(((values[i] - min_val) / range_val) * graph_height)
        y2 = 63 - int(((values[i + 1] - min_val) / range_val) * graph_height)
        oled.line(i, y1, i + 1, y2, 1)
    
    oled.text(f"{max_val:.0f}", 100, 12, 1)
    oled.text(f"{min_val:.0f}", 100, 56, 1)
    oled.show()


def draw_page_4_aqi_graph(oled):
    """Page 4: AQI graph (NaN filtered)."""
    oled.fill(0)
    oled.text("AQI        [4/7]", 0, 0, 1)
    
    all_data = logger.buffers['aqi'].get_latest(128)
    data = [(t, v) for t, v in all_data if not math.isnan(v)]
    
    if len(data) < 2:
        oled.text("Warming up...", 15, 30, 1)
        oled.show()
        return
    
    values = [v for t, v in data]
    graph_height = 51
    
    for i in range(len(data) - 1):
        # Scale x correctly after NaN filtering
        x1 = int((i / max(len(data) - 1, 1)) * 127)
        x2 = int(((i + 1) / max(len(data) - 1, 1)) * 127)
        y1 = 63 - int(((values[i] - 1) / 4) * graph_height)
        y2 = 63 - int(((values[i + 1] - 1) / 4) * graph_height)
        oled.line(x1, y1, x2, y2, 1)
    
    oled.text("5", 120, 12, 1)
    oled.text("1", 120, 56, 1)
    oled.show()


def draw_page_5_eco2_graph(oled):
    """Page 5: eCO2 graph (NaN filtered)."""
    oled.fill(0)
    oled.text("eCO2 ppm   [5/7]", 0, 0, 1)
    
    all_data = logger.buffers['eco2'].get_latest(128)
    data = [(t, v) for t, v in all_data if not math.isnan(v)]
    
    if len(data) < 2:
        oled.text("Warming up...", 15, 30, 1)
        oled.show()
        return
    
    values = [v for t, v in data]
    min_val = min(values)
    max_val = max(values)
    range_val = max(max_val - min_val, 50.0)  # Min 50ppm range
    
    graph_height = 51
    for i in range(len(data) - 1):
        x1 = int((i / max(len(data) - 1, 1)) * 127)
        x2 = int(((i + 1) / max(len(data) - 1, 1)) * 127)
        y1 = 63 - int(((values[i] - min_val) / range_val) * graph_height)
        y2 = 63 - int(((values[i + 1] - min_val) / range_val) * graph_height)
        oled.line(x1, y1, x2, y2, 1)
    
    oled.text(f"{int(max_val)}", 90, 12, 1)
    oled.text(f"{int(min_val)}", 90, 56, 1)
    oled.show()


def draw_page_6_tvoc_graph(oled):
    """Page 6: TVOC graph (NaN filtered)."""
    oled.fill(0)
    oled.text("TVOC ppb   [6/7]", 0, 0, 1)
    
    all_data = logger.buffers['tvoc'].get_latest(128)
    data = [(t, v) for t, v in all_data if not math.isnan(v)]
    
    if len(data) < 2:
        oled.text("Warming up...", 15, 30, 1)
        oled.show()
        return
    
    values = [v for t, v in data]
    min_val = min(values)
    max_val = max(values)
    range_val = max(max_val - min_val, 50.0)  # Min 50ppb range
    
    graph_height = 51
    for i in range(len(data) - 1):
        x1 = int((i / max(len(data) - 1, 1)) * 127)
        x2 = int(((i + 1) / max(len(data) - 1, 1)) * 127)
        y1 = 63 - int(((values[i] - min_val) / range_val) * graph_height)
        y2 = 63 - int(((values[i + 1] - min_val) / range_val) * graph_height)
        oled.line(x1, y1, x2, y2, 1)
    
    oled.text(f"{int(max_val)}", 90, 12, 1)
    oled.text(f"{int(min_val)}", 90, 56, 1)
    oled.show()


def draw_page_7_system_info(oled, uptime_seconds, usage_pct):
    """Page 7: System diagnostics."""
    oled.fill(0)
    oled.text("System     [7/7]", 0, 0, 1)
    oled.text("-" * 21, 0, 10, 1)
    
    hours = uptime_seconds // 3600
    mins = (uptime_seconds % 3600) // 60
    oled.text(f"Up: {hours}h {mins}m", 0, 20, 1)
    
    ram_kb = gc.mem_alloc() // 1024
    free_kb = gc.mem_free() // 1024
    oled.text(f"RAM:{ram_kb}/{ram_kb + free_kb}KB", 0, 30, 1)
    
    # Safe usage percentage instead of fragmentation test
    status = "OK" if usage_pct < 70 else "High" if usage_pct < 85 else "Full"
    oled.text(f"Usage:{usage_pct:.0f}% {status}", 0, 40, 1)
    
    stats = logger.get_stats()
    oled.text(f"Files:{stats['flash_files']}", 0, 50, 1)
    if stats['flash_days'] > 0:
        oled.text(f"Log:{stats['flash_days']:.0f}d", 70, 50, 1)
    
    oled.show()


def draw_page(oled, page_num, temp, hum, ens, data_valid, uptime, usage_pct):
    """Dispatch to page renderer."""
    if page_num == 0:
        draw_page_1_dashboard(oled, temp, hum, ens, data_valid)
    elif page_num == 1:
        draw_page_2_temp_graph(oled)
    elif page_num == 2:
        draw_page_3_hum_graph(oled)
    elif page_num == 3:
        draw_page_4_aqi_graph(oled)
    elif page_num == 4:
        draw_page_5_eco2_graph(oled)
    elif page_num == 5:
        draw_page_6_tvoc_graph(oled)
    elif page_num == 6:
        draw_page_7_system_info(oled, uptime, usage_pct)


# === MAIN LOOP ===

def main():
    """Main application loop - 20Hz responsive UI."""
    
    sys_dict = initialize_system()
    if sys_dict is None:
        return
    
    aht = sys_dict['aht']
    ens = sys_dict['ens']
    oled = sys_dict['oled']
    button = sys_dict['button']
    led = sys_dict['led']
    wdt = sys_dict['wdt']
    
    # State variables
    current_page = 0
    button_pressed = False
    just_woke = False  # Track screen wake separately
    boot_time = time.time()
    gc_counter = 0
    usage_pct = 0.0
    
    # Non-blocking timers
    last_sample_time = time.ticks_ms()
    last_button_time = time.ticks_ms()
    last_display_update = time.ticks_ms()
    screen_active = True
    
    # Sensor readings
    temp, hum, data_valid = 0.0, 0.0, False
    
    print("\nEntering main loop (20Hz)...")
    print(f"Sample interval: {DATA_SAMPLE_INTERVAL}s")
    print(f"Screen timeout: {SCREEN_TIMEOUT}s")
    print(f"Watchdog: {WATCHDOG_TIMEOUT_MS}ms")
    
    while True:
        loop_start = time.ticks_ms()
        
        # === WATCHDOG FEED (CRITICAL) ===
        wdt.feed()
        
        # === BUTTON POLLING WITH DEBOUNCE ===
        button_val = button.value()
        if button_val == 0 and not button_pressed:
            if time.ticks_diff(loop_start, last_button_time) > BUTTON_DEBOUNCE_MS:
                button_pressed = True
                last_button_time = loop_start
                
                if not screen_active:
                    # Wake screen only
                    screen_active = True
                    just_woke = True
                elif not just_woke:
                    # Normal page change
                    current_page = (current_page + 1) % TOTAL_PAGES
                
                # Immediate feedback
                if screen_active:
                    draw_page(oled, current_page, temp, hum, ens, data_valid,
                             int(time.time() - boot_time), usage_pct)
                    last_display_update = loop_start
            
        elif button_val == 1 and button_pressed:
            button_pressed = False
            just_woke = False  # Clear wake flag on release
        
        # === SCREEN SAVER ===
        inactive_ms = time.ticks_diff(loop_start, last_button_time)
        if screen_active and inactive_ms > (SCREEN_TIMEOUT * 1000):
            oled.fill(0)
            oled.show()
            screen_active = False
            print("Screen blanked (OLED protection)")
        
        # === SENSOR SAMPLING (NON-BLOCKING) ===
        sample_elapsed = time.ticks_diff(loop_start, last_sample_time)
        if sample_elapsed >= (DATA_SAMPLE_INTERVAL * 1000):
            led.on()
            
            try:
                temp, hum = aht.read_temperature_humidity()
                ens.set_compensation(temp, hum)
                data_valid = ens.update()
            except Exception as e:
                print(f"Sensor error: {e}")
                temp, hum, data_valid = 0.0, 0.0, False
            
            timestamp = time.time()
            logger.log_sample(timestamp, temp, hum, data_valid, ens)
            
            last_sample_time = loop_start
            time.sleep_ms(50)
            led.off()
        
        # === DISPLAY UPDATE (1Hz) ===
        display_elapsed = time.ticks_diff(loop_start, last_display_update)
        if screen_active and not button_pressed and display_elapsed >= DISPLAY_REFRESH_MS:
            uptime = int(time.time() - boot_time)
            draw_page(oled, current_page, temp, hum, ens, data_valid, uptime, usage_pct)
            last_display_update = loop_start
        
        # === PERIODIC GC (every 30s) ===
        gc_counter += 1
        if gc_counter >= 600:  # 600 × 50ms = 30s
            gc.collect()
            gc.threshold(gc.mem_free() // 4 + gc.mem_alloc())
            usage_pct = check_fragmentation()
            gc_counter = 0
        
        # === MAINTAIN 20Hz (50ms cycle) ===
        elapsed = time.ticks_diff(time.ticks_ms(), loop_start)
        if elapsed < 50:
            time.sleep_ms(50 - elapsed)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutdown requested")
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}")
        try:
            Pin(LED_PIN, Pin.OUT).on()  # Solid LED = crash
        except:
            pass
