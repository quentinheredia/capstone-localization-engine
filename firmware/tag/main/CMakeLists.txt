idf_component_register(
    SRCS "tag_main.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_wifi
        esp_event
        esp_netif
        nvs_flash
        esp_timer
        esp_system
)
target_link_libraries(${COMPONENT_LIB} PRIVATE m)
