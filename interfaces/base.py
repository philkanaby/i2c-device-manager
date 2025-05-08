class DeviceInterface:
    def __init__(self, address):
        self.address = address

    def read(self):
        """Read data from the device. Should be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement the read method.")

    def write(self, value, channel=None):
        """Write data to the device. Should be implemented by subclasses if applicable."""
        raise NotImplementedError("Subclasses must implement the write method if writing is supported.")