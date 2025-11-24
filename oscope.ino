#include <bluefruit.h>

static const uint8_t  NUM_CHANNELS_DEFAULT = 4;
static const uint16_t SR_DEFAULT_HZ        = 200;
static const uint16_t SR_MIN_HZ            = 10;
static const uint16_t SR_MAX_HZ            = 1000;
static const uint8_t  PIN_MAP[4]           = { A0, A1, A2, A3 };

BLEUart bleuart;

static const uint8_t PULSE_PINS[3] = { 9, 10, 11 };

static const float PULSE_FREQS_HZ[3] = {
  1.0f,
  1.0f,
  1.0f
};

typedef struct {
  uint8_t   pin;
  uint32_t  half_period_us;
  uint32_t  next_toggle_us;
  bool      state;
} PulseGen;

static PulseGen g_pulses[3];

static void init_pulses() {
  for (int i = 0; i < 3; ++i) {
    g_pulses[i].pin = PULSE_PINS[i];
    pinMode(g_pulses[i].pin, OUTPUT);
    g_pulses[i].state = false;
    digitalWrite(g_pulses[i].pin, LOW);

    float f = PULSE_FREQS_HZ[i];
    if (f <= 0.0f) f = 1.0f;
    uint32_t period_us = (uint32_t)(1000000.0f / f);
    g_pulses[i].half_period_us = period_us / 2;
    g_pulses[i].next_toggle_us = micros() + g_pulses[i].half_period_us;
  }
}

static void update_pulses() {
  uint32_t now = micros();
  for (int i = 0; i < 3; ++i) {
    if ((int32_t)(now - g_pulses[i].next_toggle_us) >= 0) {
      g_pulses[i].state = !g_pulses[i].state;
      digitalWrite(g_pulses[i].pin, g_pulses[i].state ? HIGH : LOW);

      g_pulses[i].next_toggle_us += g_pulses[i].half_period_us;

      if ((int32_t)(now - g_pulses[i].next_toggle_us) >
          (int32_t)g_pulses[i].half_period_us) {
        g_pulses[i].next_toggle_us = now + g_pulses[i].half_period_us;
      }
    }
  }
}

static volatile uint16_t g_sr_hz     = SR_DEFAULT_HZ;
static volatile uint32_t g_period_us = 1000000UL / SR_DEFAULT_HZ;
static uint8_t           g_num_ch    = NUM_CHANNELS_DEFAULT;

static uint32_t last_tx_ms = 0;

#define SAMPLE_BUF_SIZE 64

struct SampleGroup {
  uint16_t vals[4];
  uint8_t  n;
};

struct SampleRingBuffer {
  SampleGroup buf[SAMPLE_BUF_SIZE];
  uint8_t head;
  uint8_t tail;

  SampleRingBuffer() : head(0), tail(0) {}

  bool push(const int vals[4], uint8_t n) {
    if (n > 4) n = 4;
    uint8_t next = (uint8_t)((head + 1) % SAMPLE_BUF_SIZE);
    if (next == tail) {
      return false;
    }
    buf[head].n = n;
    for (uint8_t i = 0; i < n; ++i) {
      buf[head].vals[i] = (uint16_t)vals[i];
    }
    head = next;
    return true;
  }

  bool pop(SampleGroup &out) {
    if (tail == head) return false;
    out = buf[tail];
    tail = (uint8_t)((tail + 1) % SAMPLE_BUF_SIZE);
    return true;
  }
};

static SampleRingBuffer g_sample_rb;

static void send_buffered_samples() {
  SampleGroup grp;
  uint8_t max_send = 4;
  while (max_send-- && g_sample_rb.pop(grp)) {
    char line[96];
    int len = snprintf(line, sizeof(line),
                       "sr:%u", (unsigned)g_sr_hz);
    for (uint8_t ch = 0; ch < grp.n; ++ch) {
      len += snprintf(line + len, sizeof(line) - len,
                      ",ch%u:%u", ch, (unsigned)grp.vals[ch]);
    }
    len += snprintf(line + len, sizeof(line) - len, "\n");
    if (len > 0) bleuart.write((uint8_t*)line, len);
    last_tx_ms = millis();
  }
}

static char     rx_buf[48];
static uint8_t  rx_len = 0;

static inline void tx_line(const char* s) {
  bleuart.write((const uint8_t*)s, strlen(s));
}

static inline void tx_printf(const char* fmt, ...) {
  char line[96];
  va_list ap; va_start(ap, fmt);
  int n = vsnprintf(line, sizeof(line), fmt, ap);
  va_end(ap);
  if (n > 0) bleuart.write((uint8_t*)line, n);
}

static void handle_command_line(const char* line) {
  while (*line == ' ' || *line == '\t') line++;

  if (!strncmp(line, "set_sr:", 7)) {
    long v = atol(line + 7);
    if (v < SR_MIN_HZ) v = SR_MIN_HZ;
    if (v > SR_MAX_HZ) v = SR_MAX_HZ;
    
    uint32_t new_period = (v <= 0) ? g_period_us : (1000000UL / (uint32_t)v);
    noInterrupts();
    g_sr_hz     = (uint16_t)v;
    g_period_us = new_period;
    interrupts();
    tx_printf("ack:set_sr:%ld\n", v);
    return;
  }

  if (!strncmp(line, "get_sr", 6)) {
    tx_printf("sr:%u\n", (unsigned)g_sr_hz);
    return;
  }

  tx_printf("err:unknown_cmd\n");
}

static void poll_rx_commands() {
  while (bleuart.available()) {
    int c = bleuart.read();
    if (c < 0) break;
    char ch = (char)c;
    if (ch == '\r') continue;
    if (ch == '\n') {
      rx_buf[rx_len] = 0;
      if (rx_len > 0) handle_command_line(rx_buf);
      rx_len = 0;
    } else if (rx_len < sizeof(rx_buf) - 1) {
      rx_buf[rx_len++] = ch;
    } else {
      rx_len = 0;
    }
  }
}

static void start_advertising() {
  Bluefruit.Advertising.stop();
  Bluefruit.Advertising.clearData();
  Bluefruit.ScanResponse.clearData();

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(bleuart);
  Bluefruit.ScanResponse.addName();

  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
}

void connect_callback(uint16_t conn_handle) {
  (void) conn_handle;
  Bluefruit.Connection(conn_handle)->requestMtuExchange(247);
  Bluefruit.Connection(conn_handle)->requestDataLengthUpdate();
}

void disconnect_callback(uint16_t conn_handle, uint8_t reason) {
  (void) conn_handle; (void) reason;
}

void setup() {
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin();
  Bluefruit.setName("Feather ADC");
  Bluefruit.setTxPower(4);
  Bluefruit.Periph.setConnInterval(12, 24);

  bleuart.begin();
  Bluefruit.Periph.setConnectCallback(connect_callback);
  Bluefruit.Periph.setDisconnectCallback(disconnect_callback);

  analogReadResolution(12);

  start_advertising();

  for (int i = 0; i < 3; ++i) {
    pinMode(PULSE_PINS[i], OUTPUT);
    digitalWrite(PULSE_PINS[i], LOW);
  }

  init_pulses();
}

void loop() {
  poll_rx_commands();
  update_pulses();

  if (!Bluefruit.connected()) {
    waitForEvent();
    return;
  }

  static uint32_t next_us = 0;
  uint32_t period_us = g_period_us;
  uint32_t now = micros();

  if (next_us == 0) next_us = now + period_us;

  if ((int32_t)(now - next_us) >= 0) {
    next_us += period_us;
    if ((int32_t)(now - next_us) > (int32_t)period_us) {
      next_us = now + period_us;
    }

    int vals[4] = {0,0,0,0};
    uint8_t n = g_num_ch; if (n > 4) n = 4;
    for (uint8_t ch = 0; ch < n; ++ch) {
      vals[ch] = analogRead(PIN_MAP[ch]);
    }

    g_sample_rb.push(vals, n);
  }

  send_buffered_samples();

  if (millis() - last_tx_ms > 1000) {
    tx_line("hb\n");
    last_tx_ms = millis();
  }
}
}