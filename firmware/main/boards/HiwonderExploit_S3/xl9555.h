// XL9555 GPIO Expander driver (compatible with PCA9555/TCA9555)
// Minimal, IDF v5+ (new I2C master API)

#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct xl9555 *xl9555_handle_t;

typedef struct {
    i2c_master_bus_handle_t bus; // created by i2c_new_master_bus
    uint8_t i2c_addr;            // 7-bit address, default 0x20 (A2..A0 = 000)
    uint32_t scl_speed_hz;       // per-device speed, e.g. 400000
} xl9555_config_t;

// Create device on given I2C bus
esp_err_t xl9555_new(const xl9555_config_t *cfg, xl9555_handle_t *out);

// Delete device (does NOT delete the bus)
esp_err_t xl9555_del(xl9555_handle_t dev);

// Port indices: 0 -> P0 (pins 0..7), 1 -> P1 (pins 8..15)

// Read 8-bit input port value
esp_err_t xl9555_read_port(xl9555_handle_t dev, int port_index, uint8_t *val);

// Write 8-bit output port latch value
esp_err_t xl9555_write_port(xl9555_handle_t dev, int port_index, uint8_t val);

// Set configuration register (1 = input, 0 = output), per port
esp_err_t xl9555_set_port_mode(xl9555_handle_t dev, int port_index, uint8_t dir_mask);

// Read-modify-write helpers by pin index (0..15)
esp_err_t xl9555_set_pin_mode(xl9555_handle_t dev, int pin, bool input);
esp_err_t xl9555_write_pin(xl9555_handle_t dev, int pin, bool level);
esp_err_t xl9555_read_pin(xl9555_handle_t dev, int pin, bool *level);

// Polarity inversion (1 = invert), per port and per pin
esp_err_t xl9555_set_port_polarity(xl9555_handle_t dev, int port_index, uint8_t invert_mask);
esp_err_t xl9555_set_pin_polarity(xl9555_handle_t dev, int pin, bool invert);

#ifdef __cplusplus
}
#endif
