# Modifications to BabelDOC / 对 BabelDOC 的修改

## English

This directory contains [BabelDOC](https://github.com/funstory-ai/BabelDOC) v0.6.4, copyright its authors, licensed under the GNU Affero General Public License v3.0 (AGPL-3.0; see `LICENSE` here and at the repository root). It was brought into this project on 2026-10-17 and has been modified here since that date, so this is a modified version, not the upstream release. The unmodified upstream source is the commit whose message is `engine: vendor BabelDOC v0.6.4 upstream source, unmodified`: find it with `git log --grep="vendor BabelDOC v0.6.4"`, and list every change with `git diff <that commit>.. -- engine/`. The main changes, by directory:

- `babeldoc/format/pdf/document_il/midend/paragraph_finder.py`: reference-list entries split by their hanging indent, front-matter lines split, pseudocode left out of paragraphs, paragraphs joined by text line.
- `babeldoc/format/pdf/document_il/midend/il_translator.py` and `il_translator_llm_only.py`: batches carry paper context and glossary terms, first-mention glosses, headings of key concepts in Chinese only, a review pass over each batch's draft, a width rule for short labels, and hooks for pseudocode; a translation request that fails for good fails the stage instead of keeping the source text.
- `babeldoc/format/pdf/document_il/midend/typesetting.py`: CJK line spacing, smaller font-scale floor, table cells and figure labels widen into free space, labels that do not fit keep their source text.
- `babeldoc/format/pdf/document_il/midend/styles_and_formulas.py` and `utils/drawn_lines.py` (new): lines drawn as part of a formula or identifier (radical and fraction bars, underscores) stay with their glyphs.
- `babeldoc/format/pdf/document_il/utils/layout_helper.py`: word spaces inferred from the gap to the next character, scaled by font size and letter gap.
- `babeldoc/format/pdf/document_il/il_version_1.*`: the intermediate language gains `sourceComposition` and a `hanging_indent` attribute.
- `babeldoc/format/pdf/link_annotations.py` (new) and `high_level.py`: the source PDF's links are copied into the translated PDF and the source half of the side-by-side PDF.
- `babeldoc/format/pdf/translation_config.py`: new optional callbacks for the above.
- `babeldoc/docvision/doclayout.py`: on macOS the layout model runs through CoreML with a static input shape and a compiled-model cache.
- `babeldoc/babeldoc_exception/BabelDOCException.py`: `TranslationRequestError` and `TranslationRequestsFailedError`.
- `babeldoc/glossary.py`: glossary matching allows a space after a hyphen (a word broken at a line end).

## 中文

本目录是 [BabelDOC](https://github.com/funstory-ai/BabelDOC) v0.6.4 的源码，版权属其作者，许可证为 GNU Affero 通用公共许可证第 3 版（AGPL-3.0，见本目录和仓库根目录的 `LICENSE`）。它于 2026-10-17 纳入本项目，此后由本项目修改，所以这里是修改过的版本，不是上游发布版。上游原样版本是提交说明为 `engine: vendor BabelDOC v0.6.4 upstream source, unmodified` 的那个提交：用 `git log --grep="vendor BabelDOC v0.6.4"` 找到它，再用 `git diff <该提交>.. -- engine/` 列出全部修改。主要修改按目录如下：

- `babeldoc/format/pdf/document_il/midend/paragraph_finder.py`：参考文献条目按悬挂缩进分开，首页前置信息按行拆分，伪代码不并入段落，按文本行连接段落。
- `babeldoc/format/pdf/document_il/midend/il_translator.py`、`il_translator_llm_only.py`：批次带论文上下文和术语表、首次出现的括注、只用中文的关键概念标题、对每批译稿的审校、短标签的宽度规则，以及伪代码的接口；翻译请求彻底失败时，翻译阶段失败，不再保留原文。
- `babeldoc/format/pdf/document_il/midend/typesetting.py`：中文行距、更小的字号缩放下限，表格单元格和图中标签向空白处加宽，放不下的标签保留原文。
- `babeldoc/format/pdf/document_il/midend/styles_and_formulas.py`、`utils/drawn_lines.py`（新增）：作为公式或标识符一部分画出的线（根号和分数线、下划线）跟随它的字形。
- `babeldoc/format/pdf/document_il/utils/layout_helper.py`：按与下一字符的间距推断词间空格，阈值随字号和字母间距调整。
- `babeldoc/format/pdf/document_il/il_version_1.*`：中间语言新增 `sourceComposition` 和 `hanging_indent` 属性。
- `babeldoc/format/pdf/link_annotations.py`（新增）、`high_level.py`：把原 PDF 的链接复制到译文 PDF 和双语对照 PDF 的原文一侧。
- `babeldoc/format/pdf/translation_config.py`：为以上功能新增的可选回调。
- `babeldoc/docvision/doclayout.py`：在 macOS 上，版面模型用固定输入尺寸经 CoreML 运行，并缓存编译结果。
- `babeldoc/babeldoc_exception/BabelDOCException.py`：新增 `TranslationRequestError` 和 `TranslationRequestsFailedError`。
- `babeldoc/glossary.py`：术语匹配允许连字符后有空格（行末断开的单词）。
