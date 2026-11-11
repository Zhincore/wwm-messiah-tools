# WWM rich-text / chat markup cheatsheet

Source: decompiled `hexm/client/ui/controllers/richtext_controller.lua`,
`richtext_component/*.lua`, `hexm/common/misc/chat_misc.lua`.

## Scope
- Player chat text → `chat_misc.parse_raw_str_with_emotion`: only `[#id]` emotes + plain text; colour codes stripped (`strip_color_tags`).
- Full `<TAG>` grammar below → `RichTextController:process_raw_str`: system messages, item/skill descriptions, templates, UI labels.

## Grammar (process_raw_str)
- Tag: `<TYPE attr="v" attr2='v'>text</TYPE>`, regex `<([/#%u%d]+)%s*(.-)>`
- Attr: `name="value"` or `name='value'`, regex `([_%w]+)%s*=%s*(["'])(.-)%2`
- Numbered preset: `<#N>text</#N>` → row N of `richtext_tag_composition` (combine_content list of (tag_type, row), optional goto)
- Text before/after/inside tags → DEFAULT_TEXT
- Pre-pass: `#RRGGBB` lower-cased

## Components (TYPE / TAG id)
| TYPE | TAG | attrs |
|---|---|---|
| TEXT | 1 | id, color, opacity 1-255, font_name, font_size, effect1 = outline/shadow/glow/bold, effect2 = underline/strikethrough/emphasize, effect1_color, effect2_color (#rrggbb[aa]), effect_size (3), shadow_offset, italic_theta |
| IMAGE | 2 | id, image_path, image_url, color, opacity, size "W*H" (19*19), rotation -360..360, goto_id, goto_param "dict(...)", call_back |
| LINK | 3 | id, color, font_name, font_size, goto_id, goto_param, url, mobile_url, is_ng_url, is_ng_mobile_url, effect1, effect1_color, is_underline, underline_size 1-100, call_back, other_param |
| CUSTOM | 4 | id, csb_name, controller_path (inline Cocos csb node) |
| EMOTION | 5 | emotion_id, tick_count (0.2 s/frame), size "W*H" (19*19), size_type, rotation, color, opacity |

- `url` placeholders: `{uid}`, `{pid}`, `{hostnum}` → player number id, player id, host
- `call_back` / `other_param` / `goto_param`: Lua literal evaluated with `load("return "..)` (client-side code sink; only reachable from trusted/template strings)

## Inline colour
- `#RRGGBB text #E` → coloured span, `#E` ends
- Short codes → table `ui_color_attr` (`color_short` → `color`):

  | code | hex | code | hex | code | hex | code | hex | code | hex |
  |---|---|---|---|---|---|---|---|---|---|
  | `#H` | 8c5823 | `#V` | 2d7048 | `#L` | 994242 | `#D` | 808080 | `#Y` | ffc89c |
  | `#G` | aee5ae | `#R` | ed7b7b | `#Q` | 666666 | `#W` | ffc89c | `#N` | ffc89c |
  | `#A` | 5e626b | `#U` | 4e6654 | `#B` | 406182 | `#T` | 574b7a | `#X` | 826734 |
  | `#K` | aeb1b8 | `#S` | 93b8a5 | `#J` | 8bb3c7 | `#P` | aa97c7 | `#I` | d6c396 |
  | `#Z` | ffffff | `#O` | b23e41 | `#C` | abbaf5 | `#F` | c69c5f | `#M` | ccb086 |

- `ui_label_style_attr` swaps codes for light/dark variants by `label_color_style`
- Strip: `#%u` and `#rrggbb` removed for chat / plain previews

## Ruby (furigana) text
- `#ruby(base,reading)` ; coloured: `#RRGGBB ... #ruby(a,b) ... #E`

## Link shorthands (descriptions)
- Item desc: `<text|goto_id|#color>` (`process_raw_str_by_stuff_desc`)
- Skill (xinfa) desc: `<text|goto_id|#color|param1|param2...>` (extra params → goto_param "p1|p2")
- chat_misc link text: `^goto_no^` markers inside link text

## Emotes
- `[#id]` (regex `%[#(%d+)%]`), id must exist in table `chat_emotion_frame_data`
  (55 rows: `no`, `desc`, `frame_num`, `group`, `is_banned`, optional `show_frame` = static preview frame)
- Frames `ui_emotion_<id>_000<n>.png`, 0..frame_num-1, forward, tick_count 0.2 s
- Emote-only message → `is_emotion` true (bigger rendering)

## Preset tags
- `<#N>`: table `richtext_tag_composition` (10 rows): `combine_content` = [[component TAG, style row]], `goto`;
  style rows in `richtext_text_tag` / `richtext_image_tag`

## Related tables (`tools/wwm_hd.py --table NAME --locale en`)
- `chat_emotion_icon_lib` (stickers), `chat_emotion_group`, `chat_bg_popo` (bubbles), `chat_fast_words`,
  `chat_quick_reply_lib`, `chat_channel_config`, `chat_color`, `chat_input_function`

## Templates
- `{%s}` / `%s` placeholders filled by Lua `string.format` before parsing
