# Den Haag festivals for Home Assistant

Turns the City of The Hague's event-permit open data (dataset
[Evenementen Den Haag](https://den-haag-opendata.opendatasoft.com/explore/dataset/evenementendenhaag/),
also on data.overheid.nl) into an ICS calendar and sends a Friday "this weekend"
digest. denhaag.com itself blocks automated requests, so it isn't scraped.

- `denhaag/denhaag_festivals.py` goes to `/config/denhaag/` (stdlib only, runs in the HA container)
- `packages/denhaag_festivals.yaml` goes to `/config/packages/`

Requires `homeassistant: packages: !include_dir_named packages` in
`configuration.yaml`, then a restart. Add the Remote Calendar integration with
URL `http://<ha>:8123/local/denhaag_festivals.ics` and name "Den Haag festivals"
so the entity is `calendar.den_haag_festivals`. Set `notify_action` in the
Friday automation to your phone's notify action.

The dataset has no category field. The script drops withdrawn permits, private
parties, school sports days, small street and neighbourhood parties, and
standing permits longer than 180 days (weekly markets, yearly memorials). See
`EXCLUDE_RE` and `MAX_SPAN_DAYS` at the top of the script. Events are all-day
because the dataset only has dates.

The city updates the dataset irregularly (last update 2026-05-13 at the time of
writing), so upcoming events can be sparse.
