# Den Haag festivals for Home Assistant

Scrapes https://denhaag.com/nl/agenda/festivals into an ICS calendar and sends a
Friday "this weekend" digest.

- `denhaag/denhaag_festivals.py` goes to `/config/denhaag/` (stdlib only, runs in the HA container)
- `packages/denhaag_festivals.yaml` goes to `/config/packages/`

Requires `homeassistant: packages: !include_dir_named packages` in
`configuration.yaml`, then a restart. Add the Remote Calendar integration with
URL `http://<ha>:8123/local/denhaag_festivals.ics` and name "Den Haag festivals"
so the entity is `calendar.den_haag_festivals`. Set `notify_action` in the
Friday automation to your phone's notify action.
