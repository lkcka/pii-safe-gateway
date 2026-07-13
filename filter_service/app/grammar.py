"""
Построение GBNF-грамматики для constrained JSON-декодирования в llama.cpp.

КРИТИЧЕСКИ ВАЖЕН ПОРЯДОК ПОЛЕЙ В "entity": "reason" генерируется РАНЬШЕ "type"
(см. комментарий в предыдущей версии — это включает chain-of-thought до вердикта).

ВАЖНО ПРО ПРОИЗВОДИТЕЛЬНОСТЬ: поле "reason" — единственное поле со свободным
текстом произвольной длины. При GBNF-constrained декодировании каждый шаг
генерации требует проверки допустимости кандидатов из ВСЕГО словаря модели
(~150k токенов у Qwen) относительно текущего состояния грамматики — это
дорогая операция, и чем больше шагов генерации, тем выше суммарная стоимость.
Поэтому длина "reason" ограничена явным квантификатором {min,max} на уровне
грамматики, а не только текстовой просьбой в промпте — модель физически не
сможет "разогнаться" на слишком длинное рассуждение, даже если постарается.

ВАЖНЫЕ ограничения GBNF-парсера llama.cpp (проверено эмпирически):
1. В именах правил допустимы только буквы, цифры и дефис "-" (НЕ "_").
2. Тело правила не может переноситься на новую строку при скобочной глубине 0.
   Длинные правила пишем одной строкой.
"""
from llama_cpp import LlamaGrammar

from .schemas import PIIType

# Границы длины поля "reason" в СИМВОЛАХ (не токенах). Подобраны так, чтобы
# вместить одно связное предложение (8-20 слов на русском ~ 50-150 символов),
# но не позволить модели уйти в длинные рассуждения, стоимость которых
# при grammar-constrained декодировании непропорционально высока.
_REASON_MIN_CHARS = 10
_REASON_MAX_CHARS = 160

_GRAMMAR_TEMPLATE = (
    r'root ::= "{" ws "\"entities\"" ws ":" ws entity-array ws "}" ws' "\n"
    r'entity-array ::= "[" ws "]" | "[" ws entity (ws "," ws entity)* ws "]"' "\n"
    r'entity ::= "{" ws "\"start\"" ws ":" ws integer ws "," ws "\"end\"" ws ":" ws integer ws "," ws "\"text\"" ws ":" ws string ws "," ws "\"reason\"" ws ":" ws reason-string ws "," ws "\"type\"" ws ":" ws pii-type ws "}"' "\n"
    r'pii-type ::= __PII_TYPES__' "\n"
    r'string ::= "\"" char* "\""' "\n"
    r'reason-string ::= "\"" char{' + str(_REASON_MIN_CHARS) + "," + str(_REASON_MAX_CHARS) + r'} "\""' "\n"
    r'char ::= [^"\\] | "\\" escape' "\n"
    r'escape ::= ["\\/bfnrt] | "u" hex hex hex hex' "\n"
    r'hex ::= [0-9a-fA-F]' "\n"
    r'integer ::= "-"? digit+' "\n"
    r'digit ::= [0-9]' "\n"
    r'ws ::= [ \t\n]*' "\n"
)


def _pii_type_alternatives() -> str:
    """Генерирует строку GBNF-альтернатив вида "\"PERSON\"" | ... | "\"NOT_PII\"""."""
    return " | ".join(f'"\\"{member.value}\\""' for member in PIIType)


def build_pii_grammar() -> LlamaGrammar:
    """Компилирует GBNF-грамматику на основе текущего enum PIIType."""
    grammar_text = _GRAMMAR_TEMPLATE.replace(
        "__PII_TYPES__", _pii_type_alternatives()
    )
    return LlamaGrammar.from_string(grammar_text)