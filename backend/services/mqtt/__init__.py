"""
MQTTManager collaborators (see backend/services/mqtt_manager.py).

MQTTManager was a single god-class file; this package splits its inbound/
outbound concerns into cohesive mixins so each file stays reviewable, while
`backend.services.mqtt_manager.MQTTManager` remains the one class every call
site, test, and monkeypatch targets (mixins compose onto that single class
via multiple inheritance — the object graph, `self.` attribute resolution,
and instance-level monkeypatching all behave exactly as they did when every
method lived in one file). Nothing outside `mqtt_manager.py` should import
from this package directly.
"""
