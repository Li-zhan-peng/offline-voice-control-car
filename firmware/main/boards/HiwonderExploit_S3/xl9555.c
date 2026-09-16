// XL9555 GPIO Expander driver (compatible with PCA9555/TCA9555)

#include "xl9555.h"
#include <string.h>
#include "esp_check.h"

// XL9555 registers (same as PCA9555)
// Each is 2 bytes: P0 (low), P1 (high)
#define REG_INPUT      0x00
#define REG_OUTPUT     0x02
#define REG_POLARITY   0x04
#define REG_CONFIG     0x06

struct xl9555 {
    i2c_master_dev_handle_t dev;
    uint8_t addr;
};

static esp_err_t wr_reg16(i2c_master_dev_handle_t dev, uint8_t reg, uint16_t data)
{
    uint8_t buf[3] = {reg, (uint8_t)(data & 0xFF), (uint8_t)(data >> 8)};
    return i2c_master_transmit(dev, buf, sizeof(buf), 1000);
}

static esp_err_t rd_reg16(i2c_master_dev_handle_t dev, uint8_t reg, uint16_t *out)
{
    esp_err_t err = i2c_master_transmit(dev, &reg, 1, 1000);
    if (err != ESP_OK) return err;
    uint8_t buf[2];
    err = i2c_master_receive(dev, buf, sizeof(buf), 1000);
    if (err != ESP_OK) return err;
    *out = (uint16_t)buf[0] | ((uint16_t)buf[1] << 8);
    return ESP_OK;
}

esp_err_t xl9555_new(const xl9555_config_t *cfg, xl9555_handle_t *out)
{
    ESP_RETURN_ON_FALSE(cfg && out, ESP_ERR_INVALID_ARG, "xl9555", "invalid arg");

    i2c_device_config_t dev_cfg = {
        .device_address = cfg->i2c_addr,
        .scl_speed_hz = cfg->scl_speed_hz ? cfg->scl_speed_hz : 400000,
        .scl_wait_us = 0,
    };
    i2c_master_dev_handle_t dev = NULL;
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(cfg->bus, &dev_cfg, &dev), "xl9555", "add device failed");

    struct xl9555 *h = calloc(1, sizeof(*h));
    ESP_RETURN_ON_FALSE(h, ESP_ERR_NO_MEM, "xl9555", "no mem");
    h->dev = dev;
    h->addr = cfg->i2c_addr;

    // No special init needed; keep default registers
    *out = h;
    return ESP_OK;
}

esp_err_t xl9555_del(xl9555_handle_t dev)
{
    if (!dev) return ESP_OK;
    i2c_master_bus_rm_device(dev->dev);
    free(dev);
    return ESP_OK;
}

static inline esp_err_t read16(xl9555_handle_t h, uint8_t reg, uint16_t *val)
{
    return rd_reg16(h->dev, reg, val);
}

static inline esp_err_t write16(xl9555_handle_t h, uint8_t reg, uint16_t val)
{
    return wr_reg16(h->dev, reg, val);
}

esp_err_t xl9555_read_port(xl9555_handle_t dev, int port_index, uint8_t *val)
{
    if (!dev || !val || port_index < 0 || port_index > 1) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_INPUT, &v), "xl9555", "read input failed");
    *val = (port_index == 0) ? (uint8_t)(v & 0xFF) : (uint8_t)(v >> 8);
    return ESP_OK;
}

esp_err_t xl9555_write_port(xl9555_handle_t dev, int port_index, uint8_t val)
{
    if (!dev || port_index < 0 || port_index > 1) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_OUTPUT, &v), "xl9555", "read output failed");
    if (port_index == 0) {
        v = (v & 0xFF00) | val;
    } else {
        v = (v & 0x00FF) | ((uint16_t)val << 8);
    }
    return write16(dev, REG_OUTPUT, v);
}

esp_err_t xl9555_set_port_mode(xl9555_handle_t dev, int port_index, uint8_t dir_mask)
{
    if (!dev || port_index < 0 || port_index > 1) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_CONFIG, &v), "xl9555", "read cfg failed");
    if (port_index == 0) {
        v = (v & 0xFF00) | dir_mask;
    } else {
        v = (v & 0x00FF) | ((uint16_t)dir_mask << 8);
    }
    return write16(dev, REG_CONFIG, v);
}

esp_err_t xl9555_set_pin_mode(xl9555_handle_t dev, int pin, bool input)
{
    if (!dev || pin < 0 || pin > 15) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_CONFIG, &v), "xl9555", "read cfg failed");
    if (input) v |= (1u << pin); else v &= ~(1u << pin);
    return write16(dev, REG_CONFIG, v);
}

esp_err_t xl9555_write_pin(xl9555_handle_t dev, int pin, bool level)
{
    if (!dev || pin < 0 || pin > 15) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_OUTPUT, &v), "xl9555", "read out failed");
    if (level) v |= (1u << pin); else v &= ~(1u << pin);
    return write16(dev, REG_OUTPUT, v);
}

esp_err_t xl9555_read_pin(xl9555_handle_t dev, int pin, bool *level)
{
    if (!dev || !level || pin < 0 || pin > 15) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_INPUT, &v), "xl9555", "read in failed");
    *level = (v >> pin) & 1u;
    return ESP_OK;
}

esp_err_t xl9555_set_port_polarity(xl9555_handle_t dev, int port_index, uint8_t invert_mask)
{
    if (!dev || port_index < 0 || port_index > 1) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_POLARITY, &v), "xl9555", "read pol failed");
    if (port_index == 0) {
        v = (v & 0xFF00) | invert_mask;
    } else {
        v = (v & 0x00FF) | ((uint16_t)invert_mask << 8);
    }
    return write16(dev, REG_POLARITY, v);
}

esp_err_t xl9555_set_pin_polarity(xl9555_handle_t dev, int pin, bool invert)
{
    if (!dev || pin < 0 || pin > 15) return ESP_ERR_INVALID_ARG;
    uint16_t v = 0;
    ESP_RETURN_ON_ERROR(read16(dev, REG_POLARITY, &v), "xl9555", "read pol failed");
    if (invert) v |= (1u << pin); else v &= ~(1u << pin);
    return write16(dev, REG_POLARITY, v);
}
