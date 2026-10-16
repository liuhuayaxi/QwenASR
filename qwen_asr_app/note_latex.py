"""Fixed-template direct LaTeX export, with a small audited body language.

Model output is document BODY only. File access, packages, macro definitions,
dynamic control sequences and engine primitives are not part of that language.
The compiler receives copied approved images, not arbitrary course files.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata

from .note_content import clock
from .review_materials import PDF_LOCK, safe_path

TEMPLATE=Path(__file__).with_name('note_latex_template.tex')
VERSION='direct-xelatex-course-v1'
_COMMANDS=set('''CourseTitle CourseFigure SourceRef TimeRange section subsection subsubsection paragraph subparagraph
textbf textit texttt textsf textrm textnormal ttfamily textbackslash textasciitilde textasciicircum emph underline textsuperscript textsuperscript textsubscript
begin end item label ref eqref pageref footnote footnotemark footnotetext caption captionof centering raggedright
small footnotesize normalsize large Large HUGE Huge noindent par medskip smallskip bigskip vspace hspace
newline linebreak pagebreak nopagebreak newpage clearpage Needspace rule hrulefill quad qquad enspace thinspace
linewidth textwidth textheight baselineskip columnwidth arraystretch tabcolsep
toprule midrule bottomrule cmidrule multicolumn multirow hline cline addlinespace
frac dfrac tfrac cfrac sqrt binom dbinom tbinom genfrac overset underset stackrel substack
mathrm mathit mathbf mathsf mathtt mathcal mathbb mathfrak boldsymbol bm text operatorname operatornamewithlimits
left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr
hat widehat bar overline underline vec dot ddot tilde widetilde breve check acute grave boxed phantom hphantom vphantom
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa varkappa lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
sum prod coprod int iint iiint oint lim liminf limsup inf sup min max arg det gcd Pr dim ker hom deg
sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth exp log ln lg mod bmod pmod
infty partial nabla ell hbar imath jmath Re Im wp aleph emptyset varnothing
times cdot div pm mp ast star circ bullet oplus ominus otimes oslash odot
le leq ge geq neq ne approx sim simeq equiv propto cong asymp ll gg lesssim gtrsim
in notin ni subset subseteq supset supseteq cup cap setminus forall exists nexists neg land lor wedge vee
to mapsto rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow longrightarrow longleftarrow Longrightarrow Longleftarrow implies iff impliedby
uparrow downarrow updownarrow Uparrow Downarrow Updownarrow parallel perp angle triangle therefore because
cdots ldots vdots ddots dots colon vert Vert lvert rvert lVert rVert langle rangle lceil rceil lfloor rfloor
underbrace overbrace displaystyle textstyle scriptstyle scriptscriptstyle limits nolimits tag notag nonumber
SI si num degree Celsius ohm angstrom
'''.split())
_CONTROL_SYMBOLS=set('\\{}[]()%&#_$,;:! |~')
_TOKENS=re.compile(r'\\([A-Za-z@]+|[^A-Za-z@])|[{}]')
_ENVIRONMENTS=set('''equation equation* align align* aligned alignedat gather gather* gathered multline multline*
split cases matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix smallmatrix array
itemize enumerate description quote center flushleft flushright tabular tabular* tabularx longtable
ConceptBox FormulaBox ExampleBox MethodBox WarningBox'''.split())
_BOX_TITLES={'ConceptBox':'概念与定义','FormulaBox':'关键关系','ExampleBox':'例题与分析',
    'MethodBox':'解题步骤','WarningBox':'适用条件与易错点'}
_FIGURE=re.compile(r'assets/fig-[a-f0-9]{16}\.(?:png|jpe?g)\Z')


class LatexCompileError(RuntimeError):
    def __init__(self,message,*,repairable=False):
        super().__init__(message)
        self.repairable=repairable


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _without_comments(body):
    # TeX comments end at newline; a percent preceded by an even number of
    # backslashes remains a comment (\\\\% versus \\%).
    lines=[]
    for line in body.splitlines():
        for match in re.finditer('%',line):
            prefix=line[:match.start()];slashes=len(prefix)-len(prefix.rstrip('\\'))
            if slashes%2==0:line=prefix;break
        lines.append(line)
    return '\n'.join(lines)


def normalize_literal_code(body):
    """Convert plain, line-delimited verbatim to the audited text language.

    The model may use verbatim for assembly examples. Never pass that TeX
    environment through the validator: quote every character before emitting
    ordinary texttt paragraphs. Only the two delimiter lines have syntax;
    comments and commands inside the block are literal text. Other source bytes
    are unchanged so knowledge comments and source bindings remain intact.
    """
    if not isinstance(body,str) or not body.strip() or len(body)>800_000:
        raise ValueError('LaTeX 正文为空或过大')
    escapes={'\\':r'\textbackslash{}','{':r'\{','}':r'\}',
        '%':r'\%','#':r'\#','&':r'\&','$':r'\$','_':r'\_',
        '~':r'\textasciitilde{}','^':r'\textasciicircum{}',' ':r'\ '}

    def markers(line):
        values=[]
        for match in _TOKENS.finditer(line):
            if match.group(1) not in {'begin','end'}:continue
            argument=re.match(r'\s*\{verbatim\}',line[match.end():])
            if argument:values.append((match.start(),match.end()+argument.end(),match.group(1)))
        return values

    result=[];inside=False
    for line in body.splitlines(keepends=True):
        # Percent signs inside literal code cannot comment out a delimiter or
        # hide text from escaping. Outside code, follow normal TeX comments.
        active=line if inside else _without_comments(line)
        found=markers(active)
        if found:
            if len(found)!=1 or active.strip()!=active[found[0][0]:found[0][1]]:
                raise ValueError('LaTeX verbatim 起止标记必须独占一行')
            kind=found[0][2]
            if kind=='begin':
                if inside:raise ValueError('LaTeX verbatim 不允许嵌套')
                inside=True;result.append('\\begin{quote}\\small\n')
            else:
                if not inside:raise ValueError('LaTeX verbatim 结束标记缺少开始标记')
                inside=False;result.append('\\end{quote}\n')
            continue
        if inside:
            literal=line.rstrip('\r\n').expandtabs(4)
            escaped=''.join(escapes.get(char,char) for char in literal) or r'\ '
            result.append('\\noindent\\texttt{'+escaped+'}\\par\n')
        else:result.append(line)
    if inside:raise ValueError('LaTeX verbatim 环境未闭合')
    return ''.join(result)


def normalize_box_titles(body):
    """Supply the fixed template's required title without consuming body text.

    A title-less ``begin{FormulaBox}`` otherwise takes the following control
    sequence as its argument, which can fail far from the actual omission.
    This normalization is applied to source before binding/persistence; it does
    not change the template or relax the audited command language.
    """
    if not isinstance(body,str) or not body.strip() or len(body)>800_000:
        raise ValueError('LaTeX 正文为空或过大')
    # Mask comments with equal-length spaces to retain original patch offsets.
    mask=''.join(_without_comments(line)+''.join(char if char in '\r\n' else ' '
        for char in line[len(_without_comments(line)):]) for line in body.splitlines(keepends=True))
    patches=[]
    for match in _TOKENS.finditer(mask):
        if match.group(1)!='begin':continue
        environment,end=_argument(mask,match.end())
        if environment=='verbatim':raise ValueError('LaTeX verbatim 需要先规范化为字面代码')
        if environment not in _BOX_TITLES:continue
        after=end
        while after<len(mask) and mask[after].isspace():after+=1
        if after<len(mask) and mask[after]=='[':
            raise ValueError('LaTeX '+environment+' 不支持方括号选项，请使用花括号标题')
        if after==len(mask) or mask[after]!='{':patches.append((end,'{'+_BOX_TITLES[environment]+'}'))
    for position,title in reversed(patches):body=body[:position]+title+body[position:]
    return body


def _argument(text, position):
    while position<len(text) and text[position].isspace():position+=1
    if position>=len(text) or text[position]!='{':raise ValueError('LaTeX 宏参数必须使用完整花括号')
    start=position+1;depth=1;position+=1
    while position<len(text):
        char=text[position]
        if char=='\\':position+=2;continue
        if char=='{':depth+=1
        elif char=='}':
            depth-=1
            if not depth:return text[start:position],position+1
        position+=1
    raise ValueError('LaTeX 花括号未闭合')


def normalize_table_floats(body):
    """Keep generated tables beside their teaching block without float syntax.

    Only the table wrapper, its placement hint and its caption command change.
    The complete cells still pass the normal allowlist; unsafe TeX is never
    discarded to make a proposal appear valid. Raw proposals stay immutable.
    """
    if not isinstance(body,str) or not body.strip() or len(body)>800_000:
        raise ValueError('LaTeX 正文为空或过大')
    mask=''.join(_without_comments(line)+''.join(char if char in '\r\n' else ' '
        for char in line[len(_without_comments(line)):]) for line in body.splitlines(keepends=True))
    patches=[];environments=[]
    for match in _TOKENS.finditer(mask):
        command=match.group(1)
        if command in {'begin','end'}:
            environment,end=_argument(mask,match.end())
            if environment=='verbatim':raise ValueError('LaTeX verbatim 需要先规范化为字面代码')
            if command=='begin':
                if environment=='table':
                    if 'table' in environments:raise ValueError('LaTeX table 不允许嵌套')
                    patches.append((match.start(),end,r'\begin{center}'))
                    after=end
                    while after<len(mask) and mask[after].isspace():after+=1
                    if after<len(mask) and mask[after]=='[':
                        placement=re.match(r'\[[htbp!\s]+\]',mask[after:])
                        if not placement:raise ValueError('LaTeX table 位置参数无效')
                        patches.append((after,after+placement.end(),''))
                environments.append(environment)
            else:
                if not environments or environments.pop()!=environment:
                    raise ValueError('LaTeX 环境未成对闭合')
                if environment=='table':patches.append((match.start(),end,r'\end{center}'))
        elif command=='caption' and 'table' in environments:
            patches.append((match.start(),match.end(),r'\captionof{table}'))
    if environments:raise ValueError('LaTeX 环境未闭合')
    for start,end,replacement in reversed(patches):body=body[:start]+replacement+body[end:]
    return body


def normalize_course_names(body):
    """Use plain Latin spelling without modifying mathematical accent macros.

    Latin text accents are deterministic typography, not new teaching content.
    Match only a single complete letter argument; malformed or executable TeX
    remains in the body and is rejected by the normal validator.
    """
    body=re.sub(r'''\\["'`^~=](?:\{([A-Za-z])\}|([A-Za-z]))''',lambda match:match[1] or match[2],body)
    def plain(match):
        value=unicodedata.normalize('NFKD',match[0])
        return ''.join(char for char in value if not unicodedata.combining(char))
    return re.sub(r'[À-ÖØ-öø-ÿ]',plain,body)


def _validate_body(body):
    if not isinstance(body,str) or not body.strip() or len(body)>800_000:raise ValueError('LaTeX 正文为空或过大')
    if any(ord(c)<32 and c not in '\n\r\t' for c in body) or '^^' in body:raise ValueError('LaTeX 正文包含不允许的控制字符')
    clean=_without_comments(body)
    depth=0;environments=[]
    for match in _TOKENS.finditer(clean):
        token=match.group(0)
        if token.startswith('\\'):
            command=match.group(1)
            if command not in _COMMANDS and command not in _CONTROL_SYMBOLS:
                raise ValueError('LaTeX 正文不允许使用宏：\\'+command)
            if command in {'begin','end'}:
                environment,environment_end=_argument(clean,match.end())
                if environment not in _ENVIRONMENTS:raise ValueError('LaTeX 正文不允许使用环境：'+environment)
                if command=='begin':
                    if environment in _BOX_TITLES:
                        try:_argument(clean,environment_end)
                        except ValueError:raise ValueError('LaTeX '+environment+' 必须包含完整花括号标题') from None
                    environments.append(environment)
                elif not environments or environments.pop()!=environment:raise ValueError('LaTeX 环境未成对闭合')
        elif token=='{':depth+=1
        elif token=='}':
            depth-=1
            if depth<0:raise ValueError('LaTeX 花括号未成对闭合')
    if depth or environments:raise ValueError('LaTeX 正文花括号或环境未闭合')
    return clean


def prepare_document(latex_body,folder,transcript=None,images=None):
    folder=Path(folder).resolve();clean=_validate_body(latex_body)
    if sum(match.group(1)=='CourseTitle' for match in _TOKENS.finditer(clean))!=1:raise ValueError('LaTeX 正文必须恰好包含一个 CourseTitle 标题')
    if transcript is None:
        path=folder/'transcript.json';transcript=json.loads(path.read_text()) if path.exists() else {'blocks':[]}
    if images is None:
        path=folder/'images.json';images=json.loads(path.read_text()) if path.exists() else []
    blocks=transcript.get('blocks',[]) if isinstance(transcript,dict) else transcript
    if not isinstance(blocks,list) or not isinstance(images,list):raise ValueError('LaTeX 来源清单格式无效')
    if any(not isinstance(row,dict) or not isinstance(row.get('path'),str) or not _FIGURE.fullmatch(row['path']) for row in images):
        raise ValueError('LaTeX 已核对的配图清单格式无效')
    approved={row['path']:row for row in images}
    if len(approved)!=len(images):raise ValueError('LaTeX 已核对的配图清单含重复路径')
    assets={};source_refs=[]
    for match in (match for match in _TOKENS.finditer(clean) if match.group(1)=='CourseFigure'):
        relative,end=_argument(clean,match.end());caption,end=_argument(clean,end);reading,end=_argument(clean,end)
        if not _FIGURE.fullmatch(relative) or relative not in approved:raise ValueError('LaTeX 配图不在已核对的原图清单中')
        if relative in assets:raise ValueError('LaTeX 每张指定教学图只能出现一次')
        if len(caption)>300 or len(reading)>600:raise ValueError('LaTeX 图注或读图说明过长，请分到正文说明')
        image=safe_path(folder,relative)
        if not image.is_file():raise ValueError('LaTeX 配图不存在')
        digest=_sha(image)
        if approved[relative].get('sha256') and approved[relative]['sha256']!=digest:raise ValueError('LaTeX 原图版本改变，未导出')
        assets[relative]={'path':relative,'sha256':digest}
    if set(assets)!=set(approved):raise ValueError('LaTeX 正文遗漏指定教学图：'+','.join(sorted(set(approved)-set(assets))))
    replacements={}
    for match in (match for match in _TOKENS.finditer(clean) if match.group(1)=='SourceRef'):
        number,_=_argument(clean,match.end())
        if not re.fullmatch(r'[1-9][0-9]{0,4}',number) or not 1<=int(number)<=len(blocks):raise ValueError('LaTeX 引用的课堂来源编号无效')
        block=blocks[int(number)-1]
        try:start=float(block['start']);end=float(block['end'])
        except (KeyError,TypeError,ValueError):raise ValueError('LaTeX 课堂来源缺少真实起止时间') from None
        if not math.isfinite(start+end) or start<0 or end<start:raise ValueError('LaTeX 课堂来源时间无效')
        value={'source':'S'+number,'start':start,'end':end,'start_time':clock(start),'end_time':clock(end)}
        if value not in source_refs:source_refs.append(value)
        replacements[number]='\\CourseSource{'+number+'}{'+clock(start)+'}{'+clock(end)+'}'
    # Substitute only active TeX, preserving comment bytes/line endings. This
    # also supports SourceRef arguments separated by whitespace/newlines.
    mask=''.join(_without_comments(line)+''.join(char if char in '\r\n' else ' '
        for char in line[len(_without_comments(line)):]) for line in latex_body.splitlines(keepends=True))
    patches=[]
    for match in (match for match in _TOKENS.finditer(mask) if match.group(1)=='SourceRef'):
        number,end=_argument(mask,match.end());patches.append((match.start(),end,replacements[number]))
    body=latex_body
    for start,end,replacement in reversed(patches):body=body[:start]+replacement+body[end:]
    template=TEMPLATE.read_text();source=template.replace('% QWENASR_BODY',body.rstrip()+'\n')
    return {'source':source,'version':VERSION,'body_sha256':hashlib.sha256(latex_body.encode()).hexdigest(),
        'source_sha256':hashlib.sha256(source.encode()).hexdigest(),'template_sha256':_sha(TEMPLATE),
        'assets':list(assets.values()),'source_refs':source_refs}


@lru_cache(maxsize=1)
def toolchain_identity():
    engine=Path('/Library/TeX/texbin/xelatex') if sys.platform=='darwin' else Path(shutil.which('xelatex') or '')
    if not engine.is_file():raise RuntimeError('直接 LaTeX 导出需要 XeLaTeX')
    driver=engine.with_name('xdvipdfmx')
    if not driver.is_file():raise RuntimeError('直接 LaTeX 导出缺少 xdvipdfmx')
    version=subprocess.run([str(engine),'--version'],capture_output=True,text=True,timeout=10,check=True).stdout.splitlines()[0]
    root=subprocess.run([str(engine.with_name('kpsewhich')),'-var-value=TEXMFROOT'],capture_output=True,text=True,timeout=10,check=True).stdout.strip()
    fonts=[Path('/System/Library/Fonts/Supplemental/Songti.ttc'),Path('/System/Library/Fonts/Hiragino Sans GB.ttc')]
    if any(not font.is_file() for font in fonts):raise RuntimeError('直接 LaTeX 导出所需的中文字体不存在')
    return {'engine':str(engine),'driver':str(driver),'texmf_root':str(Path(root).resolve()),
        'version':version,'engine_sha256':_sha(engine),'driver_sha256':_sha(driver),
        'fonts':{str(p):_sha(p) for p in fonts if p.is_file()}}


def _sandbox(build,executable,toolchain):
    literal=lambda value:json.dumps(str(value),ensure_ascii=False)
    reads=[Path(toolchain['texmf_root']),Path('/System/Library'),Path('/System/Volumes/Preboot'),
        Path('/Library/Fonts'),Path('/usr/lib'),Path('/usr/share/locale')]
    return '(version 1)(allow default)(deny network*)(deny file-read* file-write*)(allow file-read-metadata)(allow file-read-data (literal "/"))(deny process-exec)'+\
        '(allow process-exec (literal '+literal(Path(executable).resolve())+'))'+\
        ''.join('(allow file-read* (subpath '+literal(p)+'))' for p in reads)+\
        '(allow file-read* file-write* (subpath '+literal(build)+'))'+\
        '(allow file-read* file-write* (literal "/dev/null"))(allow file-read* (literal "/dev/urandom"))'


def compile_document(body,folder,outdir,*,transcript=None,images=None):
    folder=Path(folder).resolve();build=Path(outdir).resolve()
    if not build.is_relative_to(folder):raise ValueError('LaTeX 编译目录必须位于本任务目录内')
    prepared=prepare_document(body,folder,transcript,images);tools=toolchain_identity()
    if sys.platform!='darwin' or not Path('/usr/bin/sandbox-exec').is_file():raise RuntimeError('当前系统缺少受控 LaTeX 文件沙箱，未放开编译')
    build.mkdir(parents=True,exist_ok=True)
    if any(path.is_symlink() for path in build.iterdir()):raise ValueError('LaTeX 编译目录含符号链接，未写入或执行')
    for asset in prepared['assets']:
        source=safe_path(folder,asset['path']);target=safe_path(build,asset['path']);target.parent.mkdir(exist_ok=True)
        if source!=target:shutil.copyfile(source,target)
        if _sha(target)!=asset['sha256']:raise ValueError('LaTeX 配图复制期间发生版本改变，未编译')
    source=build/'document.tex';source.write_text(prepared['source'])
    temporary=build/'.tmp';temporary.mkdir(exist_ok=True)
    env={'PATH':'/Library/TeX/texbin:/usr/bin:/bin','HOME':str(build),'TMPDIR':str(temporary),
        'LANG':'en_US.UTF-8','LC_ALL':'en_US.UTF-8','TZ':'UTC',
        'TEXMFHOME':str(build/'.texmf'),'TEXMFVAR':str(build/'.texmf-var'),
        'TEXMFCONFIG':str(build/'.texmf-config'),'TEXMFCACHE':str(build/'.texmf-cache'),
        'openin_any':'p','openout_any':'p','shell_escape':'f','SOURCE_DATE_EPOCH':'1792022400','FORCE_SOURCE_DATE':'1'}
    # The engine emits XDV only; the driver is a separately sandboxed process.
    # No shell or other child executable is authorized by either sandbox.
    commands=[[tools['engine'],'-no-shell-escape','-no-pdf','-interaction=nonstopmode','-halt-on-error','-file-line-error','-recorder','document.tex']]*2
    commands.append([tools['driver'],'-E','-o','document.pdf','document.xdv'])
    logs=[]
    with PDF_LOCK:
        (build/'document.pdf').unlink(missing_ok=True)
        (build/'document.xdv').unlink(missing_ok=True)
        for command in commands:
            try:
                result=subprocess.run(['/usr/bin/sandbox-exec','-p',_sandbox(build,command[0],tools),*command],cwd=build,env=env,capture_output=True,text=True,timeout=90)
            except subprocess.TimeoutExpired as exc:
                raise LatexCompileError('LaTeX 编译超过受控时限，源稿与日志已保留') from exc
            logs.append(result.stdout+'\n'+result.stderr)
            (build/'compile.log').write_text('\n'.join(logs))
            if result.returncode:
                diagnostic=result.stdout+'\n'+result.stderr
                body_start=TEMPLATE.read_text().split('% QWENASR_BODY')[0].count('\n')+1
                locations=[int(a or b) for a,b in re.findall(r'document\.tex:(\d+):|\bl\.(\d+)\b',diagnostic)]
                error_tail='\n'.join(diagnostic.splitlines()[-30:])
                infrastructure=re.search(r'not found|permission denied|operation not permitted|sandbox|fontspec.*(?:error|not)|font.*(?:not.*(?:load|found)|cannot)|capacity exceeded|cannot.*open',error_tail,re.I)
                repairable=bool(result.returncode>0 and command[0]==tools['engine'] and not infrastructure
                    and any(line>=body_start for line in locations))
                raise LatexCompileError('LaTeX 编译未完成（退出码 '+str(result.returncode)+'）：'+'\n'.join(diagnostic.splitlines()[-14:]),repairable=repairable)
        pdf=build/'document.pdf'
        if not pdf.is_file():raise RuntimeError('LaTeX 编译没有生成 PDF')
        from .native_pdf import pdf_operation
        pages=pdf_operation('inspect',path=str(pdf))['pages']
        if not pages:raise RuntimeError('LaTeX 导出为空')
    metadata={key:value for key,value in prepared.items() if key!='source'}
    metadata.update({'renderer':VERSION,'toolchain':tools,'toolchain_sha256':hashlib.sha256(json.dumps(tools,sort_keys=True).encode()).hexdigest(),
        'pdf_sha256':_sha(pdf),'pages':pages,'shell_escape':False,'sandbox':'macos-task-build-only-no-network'})
    return pdf,metadata
