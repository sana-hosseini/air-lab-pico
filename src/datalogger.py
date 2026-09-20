"""
Data Logger with two-tier storage: RAM buffers → Binary flash files.

TIMESTAMP BEHAVIOR:
- time.time() returns seconds since 2000-01-01 (MicroPython epoch)
- Resets on power cycle (no battery-backed RTC on Pico 2)
- Timestamps are RELATIVE - suitable for graphing and analysis
- For absolute time, add external DS3231 RTC module

BINARY FORMAT (18 bytes per sample):
- Timestamp: uint32 (4 bytes)
- Temp: float (4 bytes)
- Humidity: float (4 bytes)
- AQI: uint16 (2 bytes, range 1-5)
- eCO2: uint16 (2 bytes, range 400-65000 ppm)
- TVOC: uint16 (2 bytes, range 0-65000 ppb)
"""
import os
import time
import struct
import gc
import math
from buffers import CircularBuffer


class DataLogger:
    """
    Two-tier data logger: RAM buffers with automatic flash flushing.
    """
    
    def __init__(self, buffer_size, sample_interval=30, storage_path='/'):
        """
        Initialize synchronized buffers for 5 metrics.
        
        Args:
            buffer_size: Samples per buffer
            sample_interval: Seconds between samples
            storage_path: Flash storage root
        """
        self.buffer_size = buffer_size
        self.sample_interval = sample_interval
        self.storage_path = storage_path
        
        # Create 5 synchronized buffers
        self.buffers = {
            'temp': CircularBuffer(buffer_size),
            'hum': CircularBuffer(buffer_size),
            'aqi': CircularBuffer(buffer_size),
            'eco2': CircularBuffer(buffer_size),
            'tvoc': CircularBuffer(buffer_size)
        }
        
        # Flash management
        self.last_flush_time = time.time()
        self.flush_threshold = buffer_size // 2  # 50% trigger
        self.current_filename = self._get_next_file_number()
        self.max_file_size = self._calculate_smart_file_size()
        
        print(f"DataLogger initialized:")
        print(f"  Buffer: {buffer_size} samples ({(buffer_size*sample_interval)/3600:.1f}h)")
        print(f"  Flush at: {self.flush_threshold} samples or 15min")
        print(f"  File size: {self.max_file_size//1024}KB")
        print(f"  Current log: {self.current_filename}")

    def log_sample(self, timestamp, temp, hum, ens_valid, ens_sensor):
        """
        Central logging with synchronization across all 5 metrics.
        
        Args:
            timestamp: Unix timestamp (seconds)
            temp: Temperature (°C) from AHT21
            hum: Humidity (%) from AHT21
            ens_valid: Boolean from ens.update()
            ens_sensor: ENS160 object
        """
        # Always log temp/humidity (AHT21 always valid)
        self.buffers['temp'].append(temp, timestamp)
        self.buffers['hum'].append(hum, timestamp)
        
        # Handle ENS160 state with NaN for invalid/warming-up
        if not ens_valid or ens_sensor.warming_up:
            nan = float('nan')
            self.buffers['aqi'].append(nan, timestamp)
            self.buffers['eco2'].append(nan, timestamp)
            self.buffers['tvoc'].append(nan, timestamp)
        else:
            self.buffers['aqi'].append(float(ens_sensor.aqi), timestamp)
            self.buffers['eco2'].append(float(ens_sensor.eco2), timestamp)
            self.buffers['tvoc'].append(float(ens_sensor.tvoc), timestamp)
        
        # Check dual flush triggers
        time_trigger = (time.time() - self.last_flush_time) >= 900  # 15min
        buffer_trigger = self.buffers['temp'].count_since_last_flush >= self.flush_threshold
        
        if time_trigger or buffer_trigger:
            trigger = "time" if time_trigger else "buffer"
            print(f"Flush triggered by {trigger}")
            self.flush_to_flash()

    def flush_to_flash(self):
        """
        Write buffered data to binary file with optimized 18-byte format.
        """
        try:
            count = self.buffers['temp'].count_since_last_flush
            if count == 0:
                return
            
            print(f"Flushing {count} samples to {self.current_filename}")
            
            with open(self.current_filename, 'ab') as f:
                start_idx = (self.buffers['temp'].index - count) % self.buffer_size
                
                for i in range(count):
                    idx = (start_idx + i) % self.buffer_size
                    
                    # Extract synchronized data
                    ts = self.buffers['temp'].timestamps[idx]
                    temp = self.buffers['temp'].data[idx]
                    hum = self.buffers['hum'].data[idx]
                    aqi = self.buffers['aqi'].data[idx]
                    eco2 = self.buffers['eco2'].data[idx]
                    tvoc = self.buffers['tvoc'].data[idx]
                    
                    # Convert to uint16, preserving NaN as 65535
                    aqi_u16 = 65535 if math.isnan(aqi) else int(aqi)
                    eco2_u16 = 65535 if math.isnan(eco2) else int(eco2)
                    tvoc_u16 = 65535 if math.isnan(tvoc) else int(tvoc)
                    
                    # Pack: 18 bytes total (4+4+4+2+2+2)
                    row = struct.pack('IffHHH', ts, temp, hum, aqi_u16, eco2_u16, tvoc_u16)
                    f.write(row)
            
            # Check file size and rotate
            try:
                file_size = os.stat(self.current_filename)[6]
                print(f"File size: {file_size//1024}KB / {self.max_file_size//1024}KB")
                
                if file_size > self.max_file_size:
                    print("Rotating log file")
                    self._delete_oldest_if_needed()
                    self.current_filename = self._get_next_file_number()
                    print(f"New log: {self.current_filename}")
            except:
                pass
            
            # Reset flush counters
            for buf in self.buffers.values():
                buf.count_since_last_flush = 0
            self.last_flush_time = time.time()
            
            print("Flush complete")
            
        except OSError as e:
            print(f"FLASH ERROR: {e}")
            print("Continuing RAM-only mode")
            for buf in self.buffers.values():
                buf.count_since_last_flush = 0

    def _calculate_smart_file_size(self):
        """
        Calculate optimal file size: 7-10 days per file, min 3 files total.
        18 bytes/sample with optimized format.
        """
        try:
            stat = os.statvfs(self.storage_path)
            available_flash = stat[0] * stat[3]
            
            samples_per_day = 86400 // self.sample_interval
            target_samples = samples_per_day * 10
            ideal_size = target_samples * 18  # 18 bytes per sample
            
            max_size = available_flash // 3  # Room for 3 files minimum
            
            return min(ideal_size, max_size)
        except:
            return 500 * 1024  # 500KB fallback

    def _get_next_file_number(self):
        """Scan for existing airlab_XXX.bin files and return next number."""
        max_num = 0
        try:
            for fname in os.listdir(self.storage_path):
                if fname.startswith('airlab_') and fname.endswith('.bin'):
                    try:
                        num = int(fname[7:10])
                        max_num = max(max_num, num)
                    except:
                        pass
        except:
            pass
        
        return f"{self.storage_path}airlab_{max_num+1:03d}.bin"

    def _delete_oldest_if_needed(self):
        """Delete oldest log if flash usage >90%."""
        try:
            stat = os.statvfs(self.storage_path)
            usage = 1.0 - (stat[3] / stat[2])
            
            print(f"Flash usage: {usage*100:.1f}%")
            
            if usage > 0.90:
                print("Flash >90% - deleting oldest")
                
                files = []
                for fname in os.listdir(self.storage_path):
                    if fname.startswith('airlab_') and fname.endswith('.bin'):
                        try:
                            num = int(fname[7:10])
                            files.append((num, fname))
                        except:
                            pass
                
                if files:
                    files.sort()
                    oldest = files[0][1]
                    os.remove(f"{self.storage_path}{oldest}")
                    print(f"Deleted: {oldest}")
        except Exception as e:
            print(f"Cleanup error: {e}")
    
    def get_stats(self):
        """Return storage statistics."""
        file_count = 0
        total_bytes = 0
        
        try:
            for fname in os.listdir(self.storage_path):
                if fname.startswith('airlab_') and fname.endswith('.bin'):
                    file_count += 1
                    try:
                        total_bytes += os.stat(f"{self.storage_path}{fname}")[6]
                    except:
                        pass
        except:
            pass
        
        days_stored = (total_bytes / 18) * self.sample_interval / 86400 if total_bytes > 0 else 0
        ram_samples = self.buffers['temp'].get_count()
        ram_days = (ram_samples * self.sample_interval) / 86400
        
        return {
            'ram_capacity': self.buffer_size,
            'ram_used': ram_samples,
            'ram_percent': (ram_samples / self.buffer_size) * 100,
            'ram_days': ram_days,
            'flash_files': file_count,
            'flash_bytes': total_bytes,
            'flash_mb': total_bytes / (1024*1024),
            'flash_days': days_stored
        }
