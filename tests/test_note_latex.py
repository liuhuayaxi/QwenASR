"""Safe direct LaTeX source and an opt-in real local compiler acceptance."""
from tests import isolation  # noqa: F401

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from qwen_asr_app.note_latex import prepare_document,compile_document,normalize_literal_code,normalize_box_titles,LatexCompileError,_sandbox,toolchain_identity

TITLE=r'\CourseTitle{合成课程复习笔记}{直接 LaTeX 测试}'


class LatexSourceTests(unittest.TestCase):
    def test_missing_semantic_box_titles_are_inserted_without_consuming_body_or_comments(self):
        body=TITLE+'\n% '+r'\begin{FormulaBox}'+'\n'+r'''
\begin{FormulaBox}
% knowledge:equation
\begin{equation}DST\leftarrow DST+SRC\end{equation}
\end{FormulaBox}
\begin{ExampleBox}
\textbf{例 1：加法指令演练}
MOV AX, FIRST
\end{ExampleBox}
\begin{MethodBox}% comment before explicit title
{现有标题保留}原有步骤。\end{MethodBox}
\begin{WarningBox}
注意溢出。\end{WarningBox}
\begin{ConceptBox}定义完整。\end{ConceptBox}
'''
        normalized=normalize_box_titles(body)
        self.assertIn(r'\begin{FormulaBox}{关键关系}'+'\n% knowledge:equation',normalized)
        self.assertIn(r'\begin{ExampleBox}{例题与分析}'+'\n'+r'\textbf{例 1：加法指令演练}',normalized)
        self.assertIn(r'\begin{MethodBox}% comment before explicit title'+'\n{现有标题保留}',normalized)
        self.assertIn(r'\begin{WarningBox}{适用条件与易错点}',normalized)
        self.assertIn(r'\begin{ConceptBox}{概念与定义}定义完整。',normalized)
        self.assertIn('% '+r'\begin{FormulaBox}'+'\n',normalized)
        self.assertEqual(normalize_box_titles(normalized),normalized)
        with tempfile.TemporaryDirectory() as raw:
            prepare_document(normalized,raw,{'blocks':[]},[])

    def test_box_title_normalization_does_not_modify_literal_code_or_enable_unsafe_commands(self):
        with self.assertRaisesRegex(ValueError,'verbatim'):
            normalize_box_titles(TITLE+'\n'+r'\begin{verbatim}'+'\n'+r'\begin{FormulaBox}'+'\n'+r'\end{verbatim}')
        code=normalize_literal_code(TITLE+'\n'+r'\begin{verbatim}'+'\n'+r'\begin{FormulaBox}'+'\n'+r'\end{verbatim}')
        self.assertEqual(normalize_box_titles(code),code)
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError,'花括号标题'):
                prepare_document(TITLE+r'\begin{FormulaBox}\begin{equation}a=b\end{equation}\end{FormulaBox}',raw,{'blocks':[]},[])
            with self.assertRaisesRegex(ValueError,'方括号选项'):
                normalize_box_titles(TITLE+r'\begin{FormulaBox}[optional]{标题}a=b\end{FormulaBox}')
            with self.assertRaises(ValueError):
                prepare_document(normalize_box_titles(TITLE+r'\begin{FormulaBox}\input{private}\end{FormulaBox}'),raw,{'blocks':[]},[])

    def test_literal_assembly_preserves_indentation_lines_and_tex_special_characters(self):
        body=TITLE+'\n% knowledge:code\n'+r'''\begin{verbatim}
MOV AX, FIRST_1    ; 100% # & $ { } ~ ^
    ADD AX, SECOND

\input{/private/example.tex}
\end{quote}\write18{untrusted}
\end{verbatim}
% /knowledge:code
'''
        normalized=normalize_literal_code(body)
        self.assertIn('% knowledge:code\n',normalized)
        self.assertIn('% /knowledge:code\n',normalized)
        self.assertEqual(normalized.count(r'\noindent\texttt{'),5)
        self.assertIn(r'FIRST\_1\ \ \ \ ',normalized)
        self.assertIn(r'\%\ \#\ \&\ \$\ \{\ \}\ \textasciitilde{}\ \textasciicircum{}',normalized)
        self.assertIn(r'\noindent\texttt{\ \ \ \ ADD',normalized)
        self.assertIn(r'\noindent\texttt{\ }\par',normalized)
        self.assertIn(r'\textbackslash{}input\{/private/example.tex\}',normalized)
        self.assertIn(r'\textbackslash{}end\{quote\}\textbackslash{}write18\{untrusted\}',normalized)
        self.assertEqual(normalize_literal_code(normalized),normalized)
        with tempfile.TemporaryDirectory() as raw:
            prepared=prepare_document(normalized,raw,{'blocks':[]},[])
            self.assertEqual(prepared['body_sha256'],hashlib.sha256(normalized.encode()).hexdigest())
            self.assertEqual(prepared['assets'],[])

    def test_normalization_does_not_enable_commands_outside_code_or_comment_escape(self):
        code=r'\begin{verbatim}'+'\n'+r'% \input{private}'+'\n'+r'^^69nput{private}'+'\n'+r'\end{verbatim}'+'\n'
        with tempfile.TemporaryDirectory() as raw:
            normalized=normalize_literal_code(TITLE+'\n'+code)
            prepare_document(normalized,raw,{'blocks':[]},[])
            self.assertIn(r'\%\ \textbackslash{}input',normalized)
            self.assertNotIn('^^',normalized)
            for command in (r'\input{private}',r'\write18{shell}',r'\csname input\endcsname',r'\catcode`\%=12'):
                with self.subTest(command=command),self.assertRaises(ValueError):
                    prepare_document(normalize_literal_code(TITLE+'\n'+code+command),raw,{'blocks':[]},[])
            # Direct callers still cannot opt into a raw verbatim environment.
            with self.assertRaises(ValueError):prepare_document(TITLE+'\n'+code,raw,{'blocks':[]},[])

    def test_literal_delimiters_must_be_balanced_standalone_and_not_nested(self):
        for literal in (r'\end{verbatim}',r'\begin{verbatim}',
                '\\begin{verbatim}\n\\begin{verbatim}\n\\end{verbatim}\n',
                'prefix \\begin{verbatim}\ncode\n\\end{verbatim}\n',
                '\\begin{verbatim}\ncode\\end{verbatim}\n',
                '\\begin{verbatim}\n% \\end{verbatim}\n',
                '\\begin{verbatim}\ncode\n\\end{verbatim} \\input{private}\n'):
            with self.subTest(literal=literal),self.assertRaisesRegex(ValueError,'verbatim'):
                normalize_literal_code(TITLE+'\n'+literal)
        # Commented and escaped begin tokens retain their normal literal meaning.
        body=TITLE+'\n% \\begin{verbatim}\n'+r'\\begin{verbatim}'+'\n'
        self.assertEqual(normalize_literal_code(body),body)

    def test_harmless_scoped_monospace_declaration_does_not_weaken_macro_policy(self):
        with tempfile.TemporaryDirectory() as raw:
            body=TITLE+r'\begin{quote}\ttfamily MOV AX, FIRST\end{quote}'
            self.assertIn(body,prepare_document(body,raw,{'blocks':[]},[])['source'])
            with self.assertRaises(ValueError):
                prepare_document(body+r'\ttfamily\input{private}',raw,{'blocks':[]},[])

    def test_actual_source_times_comments_and_standard_math_are_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            body=TITLE+'\n% knowledge:k1\n'+r'\section{相位}\[\phi=\frac{2\pi}{\lambda}nL\]\(n>1\implies n^2>1\iff 1<n\impliedby n>2\) \SourceRef{1}'+'\n% /knowledge:k1\n% literal \\input{private}\n'
            value=prepare_document(body,raw,{'blocks':[{'start':72.9,'end':124.3}]},[])
            self.assertIn(r'\CourseSource{1}{00:01:12}{00:02:04}',value['source'])
            self.assertIn('% knowledge:k1\n',value['source']);self.assertIn('% /knowledge:k1\n',value['source'])
            self.assertIn('% literal \\input{private}\n',value['source'])
            self.assertEqual(value['source_refs'][0]['start'],72.9)
            self.assertEqual(value['body_sha256'],hashlib.sha256(body.encode()).hexdigest())

    def test_newline_source_argument_injects_real_times_without_changing_other_comments(self):
        with tempfile.TemporaryDirectory() as raw:
            value=prepare_document(TITLE+'\n% knowledge:k\n'+r'\SourceRef'+'\n{1}\n% /knowledge:k\n',raw,{'blocks':[{'start':1,'end':2}]},[])
            self.assertIn(r'\CourseSource{1}{00:00:01}{00:00:02}',value['source'])
            self.assertIn('% /knowledge:k',value['source'])

    def test_title_must_be_one_actual_macro_not_a_comment_or_escaped_token(self):
        with tempfile.TemporaryDirectory() as raw:
            for body in ('plain text','% '+TITLE,r'\\CourseTitle{literal}{text}',TITLE+TITLE):
                with self.subTest(body=body),self.assertRaisesRegex(ValueError,'CourseTitle'):
                    prepare_document(body,raw,{'blocks':[]},[])

    def test_semantic_formula_and_method_boxes_use_fixed_template_without_cover_or_contents(self):
        from qwen_asr_app.note_latex import TEMPLATE
        body=TITLE+r'\begin{FormulaBox}{必要关系}\[E=h\nu\]\end{FormulaBox}\begin{MethodBox}{解题思路总结}先辨明条件，再代入。\end{MethodBox}\begin{ExampleBox}{PPT原题}保留题设。\end{ExampleBox}\begin{WarningBox}{适用条件}明确满足条件。\end{WarningBox}'
        with tempfile.TemporaryDirectory() as raw:
            prepared=prepare_document(body,raw,{'blocks':[]},[])
            self.assertIn(body,prepared['source'])
            for forbidden in (r'\tableofcontents',r'\maketitle',r'\begin{titlepage}',r'\addcontentsline{toc}{section}{目录}'):
                with self.subTest(command=forbidden),self.assertRaises(ValueError):
                    prepare_document(TITLE+forbidden,raw,{'blocks':[]},[])
        template=TEMPLATE.read_text()
        self.assertIn(r'\sbox{\CourseSourceBox}{\XeTeXinterchartokenstate=1 〔课堂 S#1 · #2--#3〕}',template)
        self.assertNotIn(r'\tableofcontents',template);self.assertNotIn(r'\begin{titlepage}',template)
        self.assertIn('11pt,a4paper',template)

    def test_required_images_are_exactly_once_and_version_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'assets').mkdir();path=folder/'assets/fig-0123456789abcdef.png';path.write_bytes(b'approved synthetic image')
            images=[{'path':'assets/'+path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
            figure=r'\CourseFigure{assets/fig-0123456789abcdef.png}{原件第3页}{说明箭头和坐标。}'
            value=prepare_document(TITLE+figure,folder,{'blocks':[]},images)
            self.assertEqual(value['assets'],images)
            for body in (TITLE,TITLE+figure+figure,TITLE+figure.replace('0123456789abcdef','aaaaaaaaaaaaaaaa'),TITLE+r'\\CourseFigure{assets/fig-0123456789abcdef.png}{字面代码}{不执行}'):
                with self.subTest(body=body),self.assertRaises(ValueError):prepare_document(body,folder,{'blocks':[]},images)
            path.write_bytes(b'changed version')
            with self.assertRaisesRegex(ValueError,'版本改变'):prepare_document(TITLE+figure,folder,{'blocks':[]},images)

    def test_file_shell_definition_and_dynamic_primitive_commands_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            for command in ('input','include','openin','openout','read','write','immediate','csname','catcode','directlua','def','gdef','edef','newcommand','let','expandafter','special','pdfextension','href','includegraphics','documentclass','usepackage','scantokens','verb'):
                with self.subTest(command=command),self.assertRaises(ValueError) as caught:
                    prepare_document(TITLE+'\\'+command+'{payload}',raw,{'blocks':[]},[])
                self.assertFalse(getattr(caught.exception,'repairable',False))
            for body in (TITLE+r'\begin{document}x\end{document}',TITLE+r'\^^69nput{private}',TITLE+r'\begin{filecontents}{x}x\end{filecontents}'):
                with self.subTest(body=body),self.assertRaises(ValueError):prepare_document(body,raw,{'blocks':[]},[])

    def test_source_ref_cannot_claim_an_invented_number_or_time(self):
        with tempfile.TemporaryDirectory() as raw:
            for source,blocks in (('2',[{'start':1,'end':2}]),('0',[{'start':1,'end':2}]),('1',[{'start':5,'end':2}]),('1',[{'start':0,'end':float('inf')}])):
                with self.subTest(source=source,blocks=blocks),self.assertRaises(ValueError):
                    prepare_document(TITLE+r'\SourceRef{'+source+'}',raw,{'blocks':blocks},[])

    def test_compiler_output_path_cannot_escape_the_task(self):
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)/'task';folder.mkdir()
            with self.assertRaisesRegex(ValueError,'任务目录内'):
                compile_document(TITLE,folder,Path(raw)/'elsewhere',transcript={'blocks':[]},images=[])


@unittest.skipUnless(os.getenv('QWEN_ASR_REAL_LATEX_TEST')=='1','native direct LaTeX export is opt-in')
class LatexNativeTests(unittest.TestCase):
    def test_missing_box_title_before_equation_and_bold_example_compiles_after_normalization(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            original=TITLE+r'''
\begin{FormulaBox}
\begin{equation}\text{ADD}\quad DST,\;SRC\end{equation}
\end{FormulaBox}
\begin{ExampleBox}
\textbf{例 1：加法指令演练}
\begin{verbatim}
MOV AX, FIRST_1    ; 将低字装入寄存器
ADD AX, SECOND
\end{verbatim}
\end{ExampleBox}
'''
            body=normalize_box_titles(normalize_literal_code(original))
            pdf,_=compile_document(body,folder,folder/'build',transcript={'blocks':[]},images=[])
            with fitz.open(pdf) as document:
                text='\n'.join(page.get_text() for page in document)
            for expected in ('关键关系','例题与分析','加法指令演练','MOV AX, FIRST_1','将低字装入寄存器'):
                self.assertIn(expected,text)
            self.assertNotIn('Missing \\endcsname',(folder/'build/compile.log').read_text())

    def test_literal_code_compiles_as_text_without_reading_external_sentinel(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)/'task';folder.mkdir()
            private=Path(raw)/'outside-sentinel.tex'
            private.write_text('EXTERNAL_SENTINEL_CONTENT_MUST_NOT_APPEAR')
            literal='MOV AX, FIRST_1    ; 100% # & $ { } ~ ^\n'+r'\input{'+str(private)+'}\n'
            body=normalize_literal_code(TITLE+'\n'+r'\begin{verbatim}'+'\n'+literal+r'\end{verbatim}'+'\n')
            pdf,metadata=compile_document(body,folder,folder/'build',transcript={'blocks':[]},images=[])
            self.assertEqual(metadata['body_sha256'],hashlib.sha256(body.encode()).hexdigest())
            with fitz.open(pdf) as document:
                text='\n'.join(page.get_text() for page in document)
            self.assertIn('MOV AX, FIRST_1',text)
            self.assertIn('100%',text)
            self.assertIn(r'\input',text)
            self.assertIn('outside-sentinel.tex',text)
            self.assertNotIn('EXTERNAL_SENTINEL_CONTENT_MUST_NOT_APPEAR',text)
            # The compiler's recorded input list independently proves the path
            # was printed as text rather than opened as a TeX input file.
            self.assertNotIn(str(private),(folder/'build/document.fls').read_text())

    def test_chinese_colon_enumeration_and_multiple_source_labels_do_not_overflow(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            prose='【老师明确讲授】LED 在光纤通信与高光强工程应用中存在三大突出的物理性能瓶颈'
            body=TITLE+r'\section{中文连续引用测试}'+'\n'
            for length in (1,3,8,14,19,25,31):
                for count in (2,3):
                    body+=prose+'中文边界测试'[:length%6]+('适用条件与物理意义'*length)
                    body+=''.join(r'\SourceRef{'+str(index)+'}' for index in range(1,count+1))+'：\n'
                    body+=r'\begin{enumerate}\item 光谱较宽，光纤中的色散会使脉冲展宽。\item 光强有限，理解器件的应用条件。\end{enumerate}'+'\n'
            blocks=[{'start':index*68.5,'end':(index+1)*68.5} for index in range(3)]
            pdf,_=compile_document(body,folder,folder/'build',transcript={'blocks':blocks},images=[])
            log=(folder/'build/compile.log').read_text()
            evidence=os.getenv('QWEN_ASR_LATEX_EVIDENCE_DIR')
            if evidence:
                destination=Path(evidence);destination.mkdir(parents=True,exist_ok=True)
                (destination/'chinese-consecutive-sources.pdf').write_bytes(pdf.read_bytes())
                (destination/'chinese-consecutive-sources.log').write_text(log)
            self.assertNotIn('Overfull \\hbox',log)
            with fitz.open(pdf) as document:
                for page in document:
                    for block in page.get_text('dict')['blocks']:
                        for line in block.get('lines',[]):
                            if not any('课堂' in span['text'] for span in line['spans']):continue
                            self.assertGreaterEqual(line['bbox'][0],45)
                            self.assertLessEqual(line['bbox'][2],page.rect.width-45)
                if evidence:
                    document[0].get_pixmap(matrix=fitz.Matrix(1,1),alpha=False).save(destination/'chinese-consecutive-sources-page-001.png')

    def test_four_adjacent_source_labels_can_wrap_without_splitting_or_clipping(self):
        import fitz
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            prose='这是用于检验来源标记换行的完整说明，正文较长时仍应保持右侧页边距，连续课堂引用之间允许正常换行。'
            refs=r'\SourceRef{1}\SourceRef{2}\SourceRef{3}\SourceRef{4}'
            blocks=[{'start':number*3600+72,'end':number*3600+124} for number in range(4)]
            body=TITLE+r'\section{连续引用排版检查}'+'\n'+(prose*4+refs+'\n\n')*3
            pdf,metadata=compile_document(body,folder,folder/'build',transcript={'blocks':blocks},images=[])
            log=(folder/'build/compile.log').read_text()
            self.assertNotIn('Overfull \\hbox',log)
            with fitz.open(pdf) as document:
                text='\n'.join(page.get_text() for page in document)
                for number in range(4):
                    self.assertIn(f'{number:02d}:01:12',text);self.assertIn(f'{number:02d}:02:04',text)
                labels=[]
                for page in document:
                    for block in page.get_text('dict')['blocks']:
                        for line in block.get('lines',[]):
                            content=''.join(span['text'] for span in line['spans'])
                            if '课堂' not in content:continue
                            self.assertGreaterEqual(line['bbox'][0],45)
                            self.assertLessEqual(line['bbox'][2],page.rect.width-45)
                            labels.append(content)
                self.assertTrue(labels)
                evidence=os.getenv('QWEN_ASR_LATEX_EVIDENCE_DIR')
                if evidence:
                    destination=Path(evidence);destination.mkdir(parents=True,exist_ok=True)
                    (destination/'consecutive-sources.pdf').write_bytes(pdf.read_bytes())
                    (destination/'consecutive-sources.log').write_text(log)
                    document[0].get_pixmap(matrix=fitz.Matrix(1,1),alpha=False).save(destination/'consecutive-sources-page-001.png')

    def test_real_chinese_math_image_source_and_compiler_security(self):
        import fitz
        from PIL import Image,ImageDraw
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw);(folder/'assets').mkdir();image_path=folder/'assets/fig-0123456789abcdef.png'
            image=Image.new('RGB',(700,260),'#eaf1f5');draw=ImageDraw.Draw(image)
            draw.rectangle((40,50,650,210),outline='#244e68',width=8);draw.line((80,170,600,80),fill='#227b65',width=10);image.save(image_path)
            images=[{'path':'assets/'+image_path.name,'sha256':hashlib.sha256(image_path.read_bytes()).hexdigest()}]
            body=TITLE+r'''
\section{增益、损耗与阈值条件}
\TimeRange{00:01:12--00:02:04}
% knowledge:k1
光在谐振腔内往返传播时，增益应当补偿材料吸收、散射和镜面透射引起的损耗。先说明物理含义，再写出必要关系。\SourceRef{1}
\begin{equation}
g_{\mathrm{th}}=\alpha_i+\frac{1}{2L}\ln\frac{1}{R_1R_2}
\end{equation}
其中，$g_{\mathrm{th}}$ 是阈值增益，$L$ 是腔长，$R_1,R_2$ 是两个端面的功率反射率。\(R_1R_2<1\)。
\begin{ConceptBox}{先理解物理意义}
腔长增加可以减小镜面损耗项；结论应当与适用条件一起理解。
\end{ConceptBox}
\CourseFigure{assets/fig-0123456789abcdef.png}{合成课件第3页：谐振腔与光路}{矩形表示腔体，绿色线表示传播路径。图和说明必须出现在同一页。}
% /knowledge:k1
\subsection{符号与量纲}
\begin{tabularx}{\linewidth}{lX}
\toprule 符号 & 含义与单位 \\ \midrule
$L$ & 腔长，单位为米 \\
$\alpha_i$ & 内部损耗系数，单位为逆米 \\
\bottomrule
\end{tabularx}
'''
            pdf,metadata=compile_document(body,folder,folder/'build',transcript={'blocks':[{'start':72.9,'end':124.3}]},images=images)
            self.assertFalse(metadata['shell_escape']);self.assertIn('no-network',metadata['sandbox'])
            with fitz.open(pdf) as document:
                text='\n'.join(page.get_text() for page in document)
                self.assertIn('00:01:12',text);self.assertIn('00:02:04',text);self.assertIn('阈值',text)
                image_pages=[i for i,page in enumerate(document) if page.get_images()]
                caption_pages=[i for i,page in enumerate(document) if '合成课件第3页' in re.sub(r'\s+','',page.get_text())]
                self.assertEqual(image_pages,caption_pages);self.assertEqual(len(image_pages),1)
                evidence=os.getenv('QWEN_ASR_LATEX_EVIDENCE_DIR')
                if evidence:
                    destination=Path(evidence);destination.mkdir(parents=True,exist_ok=True)
                    (destination/'synthetic-direct-latex.pdf').write_bytes(pdf.read_bytes())
                    (destination/'acceptance.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
                    for number in sorted({0,*image_pages})[:3]:
                        document[number].get_pixmap(matrix=fitz.Matrix(1,1),alpha=False).save(destination/f'page-{number+1:03d}.png')
            with self.assertRaises(LatexCompileError) as caught:
                compile_document(TITLE+' unescaped_a',folder,folder/'syntax-error',transcript={'blocks':[]},images=[])
            self.assertTrue(caught.exception.repairable)
            sentinel=folder/'private-sentinel.tex';sentinel.write_text('PRIVATE_SENTINEL_MUST_NOT_BE_READ')
            build=folder/'sandbox-probe';build.mkdir()
            (build/'probe.tex').write_text(r'\input{'+str(sentinel)+r'}\bye')
            tools=toolchain_identity()
            result=subprocess.run(['/usr/bin/sandbox-exec','-p',_sandbox(build,tools['engine'],tools),tools['engine'],
                '-no-shell-escape','-no-pdf','-interaction=nonstopmode','-halt-on-error','probe.tex'],cwd=build,
                env={'PATH':'/usr/bin:/bin','HOME':str(build),'openin_any':'a'},capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('PRIVATE_SENTINEL_MUST_NOT_BE_READ',result.stdout+result.stderr)
            self.assertFalse((build/'probe.xdv').exists())


if __name__=='__main__':unittest.main()
