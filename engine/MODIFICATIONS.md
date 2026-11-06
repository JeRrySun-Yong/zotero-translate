# Modifications to BabelDOC / 对 BabelDOC 的修改

## English

This directory contains [BabelDOC](https://github.com/funstory-ai/BabelDOC) v0.6.4, copyright its authors, licensed under the GNU Affero General Public License v3.0 (AGPL-3.0; see `LICENSE` here and at the repository root). It was brought into this project on 2026-10-17 and has been modified here since that date, so this is a modified version, not the upstream release. The unmodified upstream source is the commit whose message is `engine: vendor BabelDOC v0.6.4 upstream source, unmodified`: find it with `git log --grep="vendor BabelDOC v0.6.4"`, and list every change with `git diff <that commit>.. -- engine/`. The main changes, one line per directory:

- `babeldoc/format/pdf/document_il/midend/`: paragraph finding (reference-list entries split at their hanging indent, front-matter lines split, a text line goes to one layout box instead of being cut by box edges, pseudocode left out of paragraphs); translation (batch prompts with paper context, key concepts kept in English by the glossary, first-mention glosses and Chinese-only key-concept headings, a review pass over each batch's draft, a width rule for short labels, an answer that repeats its input keeps the source glyphs, and a translation request that fails for good fails the stage instead of leaving the source text); typesetting (CJK line spacing 1.25 instead of 1.5, the floor of the layout search's font scale raised from 0.1 to 0.5, with a set-at-the-floor fallback and a drop below it only when no unit can be placed, one-line table cells and figure labels widened into free space, a label that does not fit its figure keeps its source text); formulas (lines drawn as part of a formula or identifier join it).
- `babeldoc/format/pdf/document_il/utils/`: word spaces inferred from a fixed threshold (0.15 em, at least 1 pt, lowered for fonts whose glyphs overlap) instead of the line's gap statistics, and a space added at a line break (not after a hyphen); a new module that assigns drawn lines (radical and fraction bars, underscores) to the glyphs they belong to.
- `babeldoc/format/pdf/document_il/`: the intermediate-language schema (`il_version_1.*`) gains `sourceComposition` (a paragraph's composition before translation) and a `hanging_indent` attribute.
- `babeldoc/format/pdf/`: a new step copies the source PDF's link annotations into the translated PDF and the source half of the side-by-side PDF; the translation configuration gains optional callbacks for the features above.
- `babeldoc/docvision/`: the layout model's CoreML session keeps its converted and compiled models in a cache directory (`~/.cache/babeldoc/coreml`), keyed by a hash of the model, so a new process does not compile them again.
- `babeldoc/babeldoc_exception/`: new exceptions `TranslationRequestError` and `TranslationRequestsFailedError`.
- `babeldoc/`: glossary matching allows a space after a hyphen (a word broken at a line end).

## 中文

本目录是 [BabelDOC](https://github.com/funstory-ai/BabelDOC) v0.6.4 的源码，版权属其作者，许可证为 GNU Affero 通用公共许可证第 3 版（AGPL-3.0，见本目录和仓库根目录的 `LICENSE`）。它于 2026-10-17 纳入本项目，此后由本项目修改，所以这里是修改过的版本，不是上游发布版。上游原样版本是提交说明为 `engine: vendor BabelDOC v0.6.4 upstream source, unmodified` 的那个提交：用 `git log --grep="vendor BabelDOC v0.6.4"` 找到它，再用 `git diff <该提交>.. -- engine/` 列出全部修改。主要修改按目录列出，一行一个目录：

- `babeldoc/format/pdf/document_il/midend/`：找段落（参考文献条目按悬挂缩进分开，首页前置信息按行拆分，一行文字归入同一个版面框而不被框边切断，伪代码不并入段落）；翻译（批次带论文上下文、术语表保留英文的关键概念、首次出现的括注、只用中文的关键概念标题，对每批译稿做审校，短标签的宽度规则，原样重复输入的译文沿用原文字形，翻译请求彻底失败时翻译阶段失败而不是保留原文）；排版（中文行距 1.25，原为 1.5；版面搜索的字号缩放下限从 0.1 提高到 0.5，放不下时按下限排版，只有一个单元都放不下时才降到下限以下；单行的表格单元格和图中标签向空白处加宽，放不进图的标签保留原文）；公式（作为公式或标识符一部分画出的线并入该公式）。
- `babeldoc/format/pdf/document_il/utils/`：词间空格按固定阈值推断（0.15 em，至少 1 pt，字形互相重叠的字体调低），不再用整行间距的统计，并在换行处补空格（连字符后不补）；新增一个模块，把画出的线（根号和分数线、下划线）归给它所属的字形。
- `babeldoc/format/pdf/document_il/`：中间语言（`il_version_1.*`）新增 `sourceComposition`（段落翻译前的内容）和 `hanging_indent` 属性。
- `babeldoc/format/pdf/`：新增一步，把原 PDF 的链接复制到译文 PDF 和双语对照 PDF 的原文一侧；翻译配置为以上功能新增可选回调。
- `babeldoc/docvision/`：版面模型的 CoreML 会话把转换和编译后的模型缓存在目录 `~/.cache/babeldoc/coreml` 中，按模型的哈希取键，新进程不必重新编译。
- `babeldoc/babeldoc_exception/`：新增异常 `TranslationRequestError` 和 `TranslationRequestsFailedError`。
- `babeldoc/`：术语匹配允许连字符后有空格（行末断开的单词）。
