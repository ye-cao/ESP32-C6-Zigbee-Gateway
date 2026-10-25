idf_component_register(
    SRCS
        "repeater.c"
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        driver
        freertos
        log
        esp_timer
        ieee802154
        espressif__esp-zigbee-lib
)
