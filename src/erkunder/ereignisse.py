"""Content-free SDK telemetry; only fixed enums and nonnegative integers leave here."""
import time

CLASSES = {'system', 'assistant', 'tool_start', 'tool_result', 'result', 'tokens', 'rate_limit', 'other'}
TOOLS = {'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'LS'}
NUMBERS = {'zeit_ns', 'input', 'output', 'cache_read', 'cache_creation', 'lesebytes'}


def clean_event(value):
    if not isinstance(value, dict) or not isinstance(value.get('klasse'), str) or value['klasse'] not in CLASSES:
        return None
    result = {'klasse': value['klasse']}
    for key in NUMBERS:
        number = value.get(key)
        if type(number) is int and number >= 0:
            result[key] = number
    if isinstance(value.get('umfang'), str) and value['umfang'] in {'modellnachricht', 'laufgesamt'}:
        result['umfang'] = value['umfang']
    if isinstance(value.get('werkzeug'), str) and value['werkzeug'] in TOOLS | {'other'}:
        result['werkzeug'] = value['werkzeug']
    return result


def snapshot(value):
    events = value.get('ereignisse', []) if isinstance(value, dict) else []
    events = [clean_event(e) for e in events] if isinstance(events, list) else []
    events = [e for e in events if e is not None]
    return {'ereignisse': events,
            'werkzeugaufrufe': sum(e['klasse'] == 'tool_start' for e in events),
            'read_antwort_bytes': sum(e.get('lesebytes', 0) for e in events)}


def usage_event(usage):
    usage = usage if isinstance(usage, dict) else {}
    return {'klasse': 'tokens', **{out: usage.get(key, 0) for out, key in (
        ('input', 'input_tokens'), ('output', 'output_tokens'),
        ('cache_read', 'cache_read_input_tokens'), ('cache_creation', 'cache_creation_input_tokens'))}}


class Observer:
    def __init__(self, emit):
        self.emit = emit
        self.tools = {}

    def send(self, event):
        self.emit({**clean_event(event), 'zeit_ns': time.time_ns()})

    def observe(self, message):
        # SDK class names are compared, never serialized as arbitrary strings.
        kind = type(message).__name__
        self.send({'klasse': {'SystemMessage': 'system', 'AssistantMessage': 'assistant',
                             'ResultMessage': 'result', 'RateLimitEvent': 'rate_limit',
                             'UserMessage': 'other'}.get(kind, 'other')})
        if kind == 'AssistantMessage':
            usage = getattr(message, '_bridge_message_usage', None)
            if isinstance(usage, dict):
                self.send({**usage_event(usage), "umfang": "modellnachricht"})
        if kind == 'ResultMessage':
            self.send({**usage_event(getattr(message, 'usage', None)), 'umfang': 'laufgesamt'})
        content = getattr(message, 'content', [])
        for block in content if isinstance(content, list) else []:
            if type(block).__name__ == 'ToolUseBlock':
                name = block.name if block.name in TOOLS else 'other'
                self.tools[block.id] = name
                self.send({'klasse': 'tool_start', 'werkzeug': name})
            elif type(block).__name__ == 'ToolResultBlock':
                name = self.tools.pop(block.tool_use_id, 'other')
                data = block.content
                # Text bytes delivered by Read, not file size or filesystem I/O.
                size = 0
                if name == 'Read':
                    if isinstance(data, str):
                        size = len(data.encode('utf-8'))
                    elif isinstance(data, list):
                        size = sum(len(item['text'].encode('utf-8')) for item in data
                                   if isinstance(item, dict) and isinstance(item.get('text'), str))
                self.send({'klasse': 'tool_result', 'werkzeug': name, 'lesebytes': size})
