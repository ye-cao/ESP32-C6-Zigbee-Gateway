idf_component_register(
    SRCS
        "repeater.c"
        "zb_ota_client.c"
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        app_update
        esp_partition
        driver
        freertos
        log
        esp_timer
        ieee802154
        espressif__esp-zigbee-lib
)
