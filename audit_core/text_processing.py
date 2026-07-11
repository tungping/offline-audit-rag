from .config import AUDIT_INPUT_TOKEN_LIMIT


def count_tokens(text):
    chinese_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    english_words = len(
        [
            word
            for word in text.split()
            if word and not any("\u4e00" <= char <= "\u9fff" for char in word)
        ]
    )
    return chinese_count + english_words


def recursive_split_text(text, chunk_size=500, chunk_overlap=200):
    separators = ["\n\n", "\n", " ", ""]

    def split(text_to_split, seps):
        if not seps:
            return [text_to_split]
        sep = seps[0]
        if sep == "":
            return [
                text_to_split[i : i + chunk_size]
                for i in range(0, len(text_to_split), chunk_size)
            ]

        parts = text_to_split.split(sep)
        chunks = []
        current_chunk = []
        current_size = 0

        for part in parts:
            part_size = count_tokens(part)
            if part_size > chunk_size:
                if current_chunk:
                    chunks.append(sep.join(current_chunk))
                    current_chunk = []
                    current_size = 0
                chunks.extend(split(part, seps[1:]))
            elif (
                current_size
                + part_size
                + (count_tokens(sep) if current_chunk else 0)
                <= chunk_size
            ):
                current_chunk.append(part)
                current_size += part_size + (
                    count_tokens(sep) if len(current_chunk) > 1 else 0
                )
            else:
                if current_chunk:
                    chunks.append(sep.join(current_chunk))
                current_chunk = [part]
                current_size = part_size
        if current_chunk:
            chunks.append(sep.join(current_chunk))
        return chunks

    raw_chunks = split(text, separators)
    merged_chunks = []
    current_group = []
    current_size = 0

    for chunk in raw_chunks:
        chunk_size_val = count_tokens(chunk)
        if current_size + chunk_size_val <= chunk_size:
            current_group.append(chunk)
            current_size += chunk_size_val
        else:
            if current_group:
                merged_chunks.append("\n".join(current_group))

            overlap_group = []
            overlap_size = 0
            for previous in reversed(current_group):
                previous_size = count_tokens(previous)
                if overlap_size + previous_size <= chunk_overlap:
                    overlap_group.insert(0, previous)
                    overlap_size += previous_size
                else:
                    break
            current_group = overlap_group + [chunk]
            current_size = overlap_size + chunk_size_val

    if current_group:
        merged_chunks.append("\n".join(current_group))

    return merged_chunks


def bound_audit_prompt_content(
    text: str, max_tokens: int = AUDIT_INPUT_TOKEN_LIMIT
) -> str:
    if count_tokens(text) <= max_tokens:
        return text

    chunks = recursive_split_text(text, chunk_size=800, chunk_overlap=0)
    if not chunks:
        return text[:max_tokens]

    notice = "【输入过长，已按原文顺序保留部分片段用于审计；中间超出上下文窗口的内容已省略。】\n"
    omitted_notice = "\n【已省略部分中间内容】\n"
    tail = chunks[-1]
    selected = [notice]
    remaining = (
        max_tokens
        - count_tokens(notice)
        - count_tokens(omitted_notice)
        - count_tokens(tail)
    )

    for chunk in chunks[:-1]:
        chunk_tokens = count_tokens(chunk)
        if chunk_tokens + 2 > remaining:
            break
        selected.append(chunk)
        remaining -= chunk_tokens + 2

    selected.extend([omitted_notice, tail])
    bounded_text = "\n\n".join(selected)

    while count_tokens(bounded_text) > max_tokens and len(selected) > 3:
        selected.pop(-3)
        bounded_text = "\n\n".join(selected)

    if count_tokens(bounded_text) > max_tokens:
        half = max(
            1,
            (
                max_tokens
                - count_tokens(notice)
                - count_tokens(omitted_notice)
            )
            // 2,
        )
        head = "".join(
            recursive_split_text(text, chunk_size=half, chunk_overlap=0)[:1]
        )
        tail = "".join(
            recursive_split_text(text, chunk_size=half, chunk_overlap=0)[-1:]
        )
        bounded_text = "\n\n".join([notice, head, omitted_notice, tail])

    return bounded_text
