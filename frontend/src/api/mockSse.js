// Mock SSE Server to simulate live telemetry data
export class MockEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.timer = null;
    this.isConnected = false;
    
    // Initial state
    this.state = {
      power_w: 0,
      current_a: 0,
      energy_kwh: 0,
      duration_sec: 0,
      cost_coins: 0,
      status: 'starting'
    };

    // Simulate connection delay
    setTimeout(() => {
      this.isConnected = true;
      this.startEmitting();
    }, 1000);
  }

  startEmitting() {
    this.state.status = 'charging';
    
    this.timer = setInterval(() => {
      // Simulate somewhat realistic charging metrics
      // e.g. drawing 11kW (11000W) approx at 16A (3 phases) or 48A single phase. Let's assume single phase 32A max, drawing 7kW.
      const current = 30 + Math.random() * 2; // 30-32 Amps
      const power = current * 230; // Watts (approx 230V)
      
      this.state.current_a = parseFloat(current.toFixed(1));
      this.state.power_w = parseFloat(power.toFixed(0));
      this.state.duration_sec += 1;
      
      // Energy = Power (kW) * Time (hours)
      const power_kw = power / 1000;
      const time_hours = 1 / 3600; // 1 second
      this.state.energy_kwh += power_kw * time_hours;
      
      // Cost (e.g. 5 coins per kWh)
      this.state.cost_coins = this.state.energy_kwh * 5;

      if (this.onmessage) {
        this.onmessage({
          data: JSON.stringify(this.state)
        });
      }
    }, 1000);
  }

  close() {
    this.isConnected = false;
    if (this.timer) {
      clearInterval(this.timer);
    }
  }
}
