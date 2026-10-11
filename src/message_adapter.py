from typing import List, Optional, Dict, Any
from src.models import Message
import re


class MessageAdapter:
    """Converts between OpenAI message format and Claude Code prompts."""
    
    @staticmethod
    def messages_to_prompt(messages: List[Message]) -> tuple[str, Optional[str]]:
        """
        Convert OpenAI messages to Claude Code prompt format.
        Returns (prompt, system_prompt)
        """
        system_prompt = None
        conversation_parts = []
        
        for message in messages:
            if message.role == "system":
                # Use the last system message as the system prompt
                system_prompt = message.content
            elif message.role == "user":
                # Handle multi-modal content (list) vs string content
                content = message.content
                if isinstance(content, list):
                    # Extract text from multi-modal content for prefix detection
                    text_content = ""
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_content += part.get("text", "")
                        elif hasattr(part, 'type') and part.type == "text":
                            text_content += getattr(part, 'text', "")
                    content_str = text_content
                else:
                    content_str = content if content else ""

                # Smart Prefix Detection: Slash commands need no "Human:" prefix
                # for SDK to recognize them (e.g., /sc:research, /sc:test, etc.)
                if content_str.strip().startswith('/'):
                    # Direct slash command - no prefix
                    conversation_parts.append(content_str)
                else:
                    # Normal conversation - with prefix
                    conversation_parts.append(f"Human: {content_str}")
            elif message.role == "assistant":
                conversation_parts.append(f"Assistant: {message.content}")
        
        # Join conversation parts
        prompt = "\n\n".join(conversation_parts)
        
        # If the last message wasn't from the user, add a prompt for assistant
        if messages and messages[-1].role != "user":
            prompt += "\n\nHuman: Please continue."
            
        return prompt, system_prompt
    
    @staticmethod
    def filter_content(content: str) -> str:
        """
        Filter content for unsupported features and tool usage.
        Remove thinking blocks, tool calls, and image references.
        """
        if not content:
            return content
            
        # Remove thinking blocks (common when tools are disabled but Claude tries to think)
        thinking_pattern = r'<thinking>.*?</thinking>'
        content = re.sub(thinking_pattern, '', content, flags=re.DOTALL)
        
        # Extract content from attempt_completion blocks (these contain the actual user response)
        attempt_completion_pattern = r'<attempt_completion>(.*?)</attempt_completion>'
        attempt_matches = re.findall(attempt_completion_pattern, content, flags=re.DOTALL)
        if attempt_matches:
            # Use the content from the attempt_completion block
            extracted_content = attempt_matches[0].strip()
            
            # If there's a <result> tag inside, extract from that
            result_pattern = r'<result>(.*?)</result>'
            result_matches = re.findall(result_pattern, extracted_content, flags=re.DOTALL)
            if result_matches:
                extracted_content = result_matches[0].strip()
            
            if extracted_content:
                content = extracted_content
        else:
            # Remove other tool usage blocks (when tools are disabled but Claude tries to use them)
            tool_patterns = [
                r'<read_file>.*?</read_file>',
                r'<write_file>.*?</write_file>',
                r'<bash>.*?</bash>',
                r'<search_files>.*?</search_files>',
                r'<str_replace_editor>.*?</str_replace_editor>',
                r'<args>.*?</args>',
                r'<ask_followup_question>.*?</ask_followup_question>',
                r'<attempt_completion>.*?</attempt_completion>',
                r'<question>.*?</question>',
                r'<follow_up>.*?</follow_up>',
                r'<suggest>.*?</suggest>',
            ]
            
            for pattern in tool_patterns:
                content = re.sub(pattern, '', content, flags=re.DOTALL)
        
        # NOTE: Image handling moved to VisionProvider
        # Images are now routed to direct Anthropic API instead of being filtered out
        # Only remove image placeholders that were already processed
        placeholder_pattern = r'\[Image: Content not supported by Claude Code\]'
        content = re.sub(placeholder_pattern, '', content)
        
        # Clean up extra whitespace and newlines
        content = MessageAdapter._normalize_whitespace(content)

        # Defensive: If content is empty after filtering, raise error (no silent fallback)
        if not content or content.isspace():
            raise ValueError("Message content is empty after filtering. Check if Claude responded with only tool usage or unsupported content.")

        return content
    
    @staticmethod
    def _normalize_whitespace(content: str) -> str:
        """Collapse runs of 3+ newlines to two, strip the ends.

        A cache boundary marker (cache_boundary.SENTINEL, exactly once) is
        invisible to this step: the text is normalized WITHOUT it and the marker
        goes back in at the matching place. Otherwise a marker inside a newline
        run or after a trailing newline breaks the run and the CLI gets one
        newline more than without a boundary (CACHE4R, 18 of 61 cases). Zero or
        several markers: unchanged behaviour, split_prompt reports the loss."""
        from src.cache_boundary import SENTINEL

        pos = content.find(SENTINEL)
        if pos < 0 or content.count(SENTINEL) != 1:
            return re.sub(r'\n\s*\n\s*\n', '\n\n', content).strip()
        text = content[:pos] + content[pos + len(SENTINEL):]
        out = []
        last = 0
        new_pos = None
        for match in re.finditer(r'\n\s*\n\s*\n', text):
            if new_pos is None and pos < match.start():
                new_pos = sum(len(part) for part in out) + (pos - last)
            out.append(text[last:match.start()])
            if new_pos is None and pos < match.end():
                new_pos = sum(len(part) for part in out) + min(pos - match.start(), 2)
            out.append('\n\n')
            last = match.end()
        if new_pos is None:
            new_pos = sum(len(part) for part in out) + (pos - last)
        out.append(text[last:])
        collapsed = ''.join(out)
        stripped = collapsed.lstrip()
        new_pos = max(0, new_pos - (len(collapsed) - len(stripped)))
        stripped = stripped.rstrip()
        new_pos = min(new_pos, len(stripped))
        return stripped[:new_pos] + SENTINEL + stripped[new_pos:]

    @staticmethod
    def format_claude_response(content: str, model: str, finish_reason: str = "stop") -> Dict[str, Any]:
        """Format Claude response for OpenAI compatibility."""
        return {
            "role": "assistant",
            "content": content,
            "finish_reason": finish_reason,
            "model": model
        }
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Rough estimation of token count.
        OpenAI's rule of thumb: ~4 characters per token for English text.
        """
        return len(text) // 4