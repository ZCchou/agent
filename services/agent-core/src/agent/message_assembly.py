"""Place application instructions before conversational data and tool exchanges."""
from typing import Any, Iterable

from agent.storage import CoreDataError


def insert_system_message(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    if message.get('role') != 'system':
        raise CoreDataError('invocation_payload_invalid', 'Expected an application system instruction.')
    index = next((i for i, item in enumerate(messages) if item.get('role') != 'system'), len(messages))
    messages.insert(index, dict(message))


def extend_application_messages(messages: list[dict[str, Any]], additions: Iterable[dict[str, Any]]) -> None:
    """Use only for application-generated additions or authorized saved checkpoints."""
    for message in list(additions):
        if message.get('role') == 'system':
            insert_system_message(messages, message)
        elif (message.get('role') == 'assistant' and message.get('_working_notes')
                and not message.get('tool_calls')):
            # Derived memory precedes conversational data. It remains assistant
            # material, never a policy or the latest answer to the user's turn.
            index = next((i for i, item in enumerate(messages) if item.get('role') != 'system'), len(messages))
            while (index < len(messages) and messages[index].get('role') == 'assistant'
                    and messages[index].get('_working_notes') and not messages[index].get('tool_calls')):
                index += 1
            messages.insert(index, dict(message))
        else:
            messages.append(dict(message))


def validate_system_message_prefix(messages: list[dict[str, Any]]) -> None:
    conversation_started = False
    for index, message in enumerate(messages):
        if message.get('role') != 'system':
            conversation_started = True
        elif conversation_started:
            roles = {'system', 'user', 'assistant', 'tool', 'developer', 'function'}
            raise CoreDataError('model_message_order_invalid',
                'System instructions must precede user, assistant and tool messages.',
                {'phase': 'message_compilation', 'message_index': index,
                 'message_roles': [item.get('role') if isinstance(item.get('role'), str)
                                   and item['role'] in roles else 'unknown' for item in messages]})
