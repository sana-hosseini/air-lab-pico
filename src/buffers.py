"""
Circular buffer for Air Lab Pico Edition.
Pre-allocated arrays prevent memory fragmentation.
"""
import array


class CircularBuffer:
    """
    Fixed-size circular buffer with generator-based allocation.
    Stores float values and integer timestamps in separate arrays.
    """
    
    def __init__(self, size):
        """Initialize with pre-allocated arrays (no temporary objects)."""
        self.size = size
        
        # Generator allocation prevents heap fragmentation
        self.data = array.array('f', (0.0 for _ in range(size)))
        self.timestamps = array.array('I', (0 for _ in range(size)))
        
        self.index = 0
        self.full = False
        self.count_since_last_flush = 0
        
    def append(self, value, timestamp):
        """Add value+timestamp (use float('nan') for invalid data)."""
        self.data[self.index] = value
        self.timestamps[self.index] = int(timestamp)
        self.index = (self.index + 1) % self.size
        if self.index == 0:
            self.full = True
        self.count_since_last_flush += 1
            
    def get_latest(self, n):
        """
        Return last n samples in chronological order.
        
        Returns:
            List of (timestamp, value) tuples
        """
        result = []
        count = self.size if self.full else self.index
        actual_n = min(n, count)
        
        if actual_n == 0:
            return result
        
        start = (self.index - actual_n) % self.size
        
        for i in range(actual_n):
            idx = (start + i) % self.size
            result.append((self.timestamps[idx], self.data[idx]))
        
        return result
    
    def get_count(self):
        """Return current number of samples."""
        return self.size if self.full else self.index
