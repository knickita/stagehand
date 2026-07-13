import time


class PowerLineProfiler:
    def __init__(self, label="PowerLines", enabled=True):
        self.label = label
        self.enabled = bool(enabled)
        self.start_time = time.perf_counter()
        self.last_time = self.start_time
        if self.enabled:
            print(f"[{self.label} +0.000s | 0.000s] start")

    def step(self, name, **metrics):
        if not self.enabled:
            return

        now = time.perf_counter()
        total = now - self.start_time
        delta = now - self.last_time
        self.last_time = now
        metric_text = self._format_metrics(metrics)
        print(f"[{self.label} +{total:.3f}s | {delta:.3f}s] {name}{metric_text}")

    @staticmethod
    def _format_metrics(metrics):
        if not metrics:
            return ""
        parts = []
        for key, value in metrics.items():
            parts.append(f"{key}={value}")
        return " (" + ", ".join(parts) + ")"