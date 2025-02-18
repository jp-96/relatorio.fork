# This file is part of relatorio.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import re

try:
    # requires python 2.5+
    from hashlib import md5
except ImportError:
    from md5 import md5

import base64
import datetime
import mimetypes
import sys
import time
import urllib.parse
import warnings
import zipfile
from copy import deepcopy
from decimal import Decimal
from io import BytesIO

import genshi
import genshi.output
import lxml.etree
from genshi.core import Stream
from genshi.filters import Transformer
from genshi.filters.transform import ENTER, EXIT
from genshi.template import MarkupTemplate
from genshi.template.interpolation import PREFIX

import relatorio
from relatorio.reporting import MIMETemplateLoader, Report
from relatorio.templates.base import RelatorioStream

try:
    from relatorio.templates.chart import Template as ChartTemplate
except ImportError:
    ChartTemplate = None

__metaclass__ = type
warnings.filterwarnings('always', module='relatorio.templates.opendocument')

GENSHI_EXPR = re.compile(r'''
        (/)?                                 # is this a closing tag?
        (for|if|choose|when|otherwise|with|
         attrs|content|replace|strip)        # tag directive
        \s*
        (?:\s([\w:-]+)=["'](.*)["']|$)       # match a single attr & its value
        |
        .*                                   # or anything else
        ''', re.VERBOSE)
GENSHI_CLOSING_DIRECTIVE = ['for',
                            'if',
                            'choose',
                            'when',
                            'otherwise',
                            'with',
                            ]

RELATORIO_URI = 'relatorio'
GENSHI_URI = 'http://genshi.edgewall.org/'
MANIFEST = 'META-INF/manifest.xml'
META = 'meta.xml'
THUMBNAILS = 'Thumbnails'
output_encode = genshi.output.encode
EtreeElement = lxml.etree.Element
XML_INVALID_CHAR_EXPR = re.compile(
    # from https://www.w3.org/TR/REC-xml/#charsets
    '[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]')

# A note regarding OpenDocument namespaces:
#
# The current code assumes the original OpenOffice document uses default
# namespace prefix ("table", "xlink", "draw", ...). We derive the actual
# namespaces URIs from their prefix, instead of the other way round. This has
# the advantage that if a new version of the format use different namespaces
# (this is not the case for ODF 1.1 but could be the case in the future since
# there is a version number in those namespaces after all), Relatorio will
# support those new formats out of the box.


# A note about attribute namespaces:
#
# Ideally, we should update the namespace map of all the nodes we add
# (Genshi) attributes to, so that the attributes use a nice "py" prefix instead
# of a generated one (eg. "ns0", which is correct but ugly) in the case no
# parent node defines it. Unfortunately, lxml doesn't support this:
# the nsmap attribute of Element objects is (currently) readonly.


class OOTemplateError(genshi.template.base.TemplateSyntaxError):
    "Error to raise when there is a SyntaxError in the genshi template"


class ImageHref:
    "A class used to add images in the odf zipfile"

    def __init__(self, serializer, context):
        self.serializer = serializer
        self.context = context.copy()

    def __call__(self, expr):
        bitstream, mimetype = expr[:2]
        if isinstance(bitstream, Report):
            bitstream = bitstream(**self.context).render()
        elif ChartTemplate and isinstance(bitstream, ChartTemplate):
            bitstream = bitstream.generate(**self.context).render()
        elif not hasattr(bitstream, 'seek') or not hasattr(bitstream, 'read'):
            bitstream = BytesIO(bitstream)
        if bitstream:
            bitstream.seek(0)
            file_content = bitstream.read()
        else:
            file_content = b''
        name = md5(file_content).hexdigest()
        path = 'Pictures/%s%s' % (
            name, mimetypes.guess_extension(mimetype or '') or '')
        self.serializer.add_file(path, file_content, mimetype)
        return {'{http://www.w3.org/1999/xlink}href': path}


class ImageDimension:
    "A class used to set dimension in draw tags"

    def __init__(self, namespaces):
        self.namespaces = namespaces

    def __call__(self, expr, width, height):
        # expr could be (bitstream, mimetype)
        # or (bitstreamm mimetype, width, height)
        if len(expr) >= 4:
            width, height = (
                i or j for i, j in zip(expr[2:4], [width, height]))
        attrs = {}
        if width:
            attrs['{%s}width' % self.namespaces['svg']] = width
        if height:
            attrs['{%s}height' % self.namespaces['svg']] = height
        return attrs


class ColumnCounter:
    """A class used to count the actual maximum number of cells (and thus
    columns) a table contains accross its rows.
    """
    def __init__(self):
        self.temp_counters = {}
        self.counters = {}

    def reset(self, loop_id):
        self.temp_counters[loop_id] = 0

    def inc(self, loop_id):
        self.temp_counters[loop_id] += 1

    def store(self, loop_id, table_name):
        self.counters[table_name] = max(self.temp_counters.pop(loop_id),
                                        self.counters.get(table_name, 0))


class ExpressionCache:
    """A class used to cache result of expression evaluation"""
    def __init__(self):
        self.cache = {}

    def store(self, expression_id, expression):
        self.cache[expression_id] = expression
        return expression

    def get(self, expression_id):
        return self.cache[expression_id]


def wrap_nodes_between(first, last, new_parent):
    """An helper function to move all nodes between two nodes to a new node
    and add that new node to their former parent. The boundary nodes are
    removed in the process.
    """
    old_parent = first.getparent()

    # Any text after the opening tag (and not within a tag) need to be handled
    # explicitly. For example in <if>xxx<span>yyy</span>zzz</if>, zzz is
    # copied along the span tag, but not xxx, which corresponds to the tail
    # attribute of the opening tag.
    if first.tail:
        new_parent.text = first.tail
    for node in first.itersiblings():
        if node is last:
            break
        # appending a node to a new parent also
        # remove it from its previous parent
        new_parent.append(node)
    old_parent.replace(first, new_parent)
    new_parent.tail = last.tail
    old_parent.remove(last)


def remove_node_keeping_tail(node):
    """Remove the node from the tree but keeping tail by appending to the
    previous or parent node.
    """
    parent = node.getparent()
    if node.tail:
        previous = node.getprevious()
        if previous is not None:
            if not previous.tail:
                previous.tail = node.tail
            else:
                previous.tail += node.tail
        else:
            if not parent.text:
                parent.text = node.tail
            else:
                parent.text += node.tail
    parent.remove(node)


def update_py_attrs(node, value):
    """An helper function to update py_attrs of a node.
    """
    if not value:
        return
    py_attrs_attr = '{%s}attrs' % GENSHI_URI
    if py_attrs_attr not in node.attrib:
        node.attrib[py_attrs_attr] = value
    else:
        node.attrib[py_attrs_attr] = \
                "(lambda x, y: x.update(y) or x)(%s or {}, %s or {})" % \
                (node.attrib[py_attrs_attr], value)


def escape_xml_invalid_chars(value, repl=' '):
    "Replace invalid characters for XML."
    if isinstance(value, str):
        return XML_INVALID_CHAR_EXPR.sub(repl, value)
    else:
        return value


class Template(MarkupTemplate):

    def __init__(self, source, filepath=None, filename=None, loader=None,
                 encoding=None, lookup='strict', allow_exec=True):
        # assign default/fake namespaces so that documents do not need to
        # define them if they don't use them
        self.namespaces = {
            "text": "urn:text",
            "draw": "urn:draw",
            "table": "urn:table",
            "office": "urn:office",
            "xlink": "urn:xlink",
            "svg": "urn:svg",
        }
        self.inner_docs = []
        self.has_col_loop = False
        self._source = None
        self._files = set()
        super(Template, self).__init__(source, filepath, filename, loader,
                                       encoding, lookup, allow_exec)

    def _parse(self, source, encoding):
        """parses the odf file.

        It adds genshi directives and finds the inner docs.
        """
        if not self.filepath:
            if hasattr(source, 'read') and hasattr(source, 'mode'):
                if 'U' in source.mode:
                    # TemplateLoader of Genshi <= 0.6 open files with universal
                    # newlines which is not suitable for zipfile
                    raise ValueError('filepath is required '
                        'if source is openned with universal newlines')
                else:
                    # source could be closed before generate calls
                    source = BytesIO(source.read())
        else:
            source = self.filepath
        self._source = source
        self.filepath = None  # Prevent zip content in traceback
        zf = get_zip_file(source)
        content = zf.read('content.xml')
        styles = zf.read('styles.xml')
        meta = zf.read('meta.xml')

        template = super(Template, self)
        content = template._parse(self.insert_directives(content), encoding)
        styles = template._parse(self.insert_directives(styles), encoding)
        meta = template._parse(self.insert_directives(meta), encoding)
        content_files = [('content.xml', content)]
        styles_files = [('styles.xml', styles)]
        meta_files = [('meta.xml', meta)]

        while self.inner_docs:
            doc = self.inner_docs.pop()
            c_path, s_path = (
                doc + '/content.xml', doc + '/styles.xml')
            content = zf.read(c_path)
            styles = zf.read(s_path)

            c_parsed = template._parse(self.insert_directives(content),
                                       encoding)
            s_parsed = template._parse(self.insert_directives(styles),
                                       encoding)
            content_files.append((c_path, c_parsed))
            styles_files.append((s_path, s_parsed))
        zf.close()

        parsed = []
        for fpath, fparsed in content_files + styles_files + meta_files:
            self._files.add(fpath)
            parsed.append((genshi.core.PI, ('relatorio', fpath), None))
            parsed += fparsed

        return parsed

    def insert_directives(self, content):
        """adds the genshi directives, handle the images and the innerdocs.
        """
        tree = lxml.etree.parse(BytesIO(content))
        root = tree.getroot()

        # but override them with the real namespaces
        self.namespaces.update(root.nsmap)

        # remove any "root" namespace as lxml.xpath do not support them
        self.namespaces.pop(None, None)

        self.namespaces['py'] = GENSHI_URI
        self.namespaces['relatorio'] = RELATORIO_URI

        self._remove_soft_page_break(tree)
        self._invert_style(tree)
        self._handle_meta(tree)
        self._handle_relatorio_tags(tree)
        self._handle_images(tree)
        self._handle_innerdocs(tree)
        self._escape_values(tree)
        return BytesIO(lxml.etree.tostring(tree))

    def _remove_soft_page_break(self, tree):
        "remove soft-page-break tag and use-soft-page-break attribute"
        xpath_expr = "//text:soft-page-break"
        for node in tree.xpath(xpath_expr, namespaces=self.namespaces):
            remove_node_keeping_tail(node)

        xpath_expr = "//office:text[@text:use-soft-page-breaks]"
        text_namespace = self.namespaces['text']
        for node in tree.xpath(xpath_expr, namespaces=self.namespaces):
            node.attrib.pop('{%s}use-soft-page-breaks' % text_namespace)

    def _invert_style(self, tree):
        "inverts the text:a and text:span"
        xpath_expr = "//text:a[starts-with(@xlink:href, 'relatorio://')]" \
                     "/text:span"
        for span in tree.xpath(xpath_expr, namespaces=self.namespaces):
            text_a = span.getparent()
            outer = text_a.getparent()
            text_a.text = span.text
            span.text = ''
            text_a.remove(span)
            outer.replace(text_a, span)
            span.append(text_a)

    def _relatorio_statements(self, tree):
        "finds the relatorio statements (text:a/text:placeholder)"
        # If this node href matches the relatorio URL it is kept.
        # If this node href matches a genshi directive it is kept for further
        # processing.
        xlink_href_attrib = '{%s}href' % self.namespaces['xlink']
        text_a = '{%s}a' % self.namespaces['text']
        placeholder = '{%s}placeholder' % self.namespaces['text']
        s_xpath = "//text:a[starts-with(@xlink:href, 'relatorio://')]" \
                  "| //text:placeholder"

        r_statements = []
        opened_tags = []
        # We map each opening tag with its closing tag
        closing_tags = {}
        for statement in tree.xpath(s_xpath, namespaces=self.namespaces):
            if statement.tag == placeholder:
                expr = statement.text[1:-1]
            elif statement.tag == text_a:
                expr = urllib.parse.unquote(
                    statement.attrib[xlink_href_attrib][12:])

            if not expr:
                raise OOTemplateError("No expression in the tag",
                                      self.filepath)
            closing, directive, attr, attr_val = \
                GENSHI_EXPR.match(expr).groups()
            is_opening = closing != '/'

            warn_msg = None
            if not statement.text:
                warn_msg = "No statement text in '%s' for '%s'" \
                           % (self.filepath, expr)
            elif (directive != 'attrs'
                    and expr != statement.text
                    and statement.tag == text_a):
                warn_msg = "url and text do not match in %s: %s != %s" \
                           % (self.filepath, expr, statement.text)
            if warn_msg:
                if directive is not None and not is_opening:
                    warn_msg += " corresponding to opening tag '%s'" \
                                % opened_tags[-1][0].text
                warnings.warn(warn_msg)

            if directive in GENSHI_CLOSING_DIRECTIVE:
                # map closing tags with their opening tag
                if is_opening:
                    opened_tags.append((statement, directive))
                else:
                    opening_statement, opening_directive = opened_tags.pop()
                    assert directive == opening_directive, (
                        "Wrong pairing tags between '%s' and '%s'"
                        % (opening_statement.text.encode('utf-8'),
                           statement.text.encode('utf-8')))
                    closing_tags[id(opening_statement)] = statement
            # - we only need to return opening statements
            if is_opening:
                r_statements.append((statement,
                                     (expr, directive, attr, attr_val))
                                   )
        assert not opened_tags
        return r_statements, closing_tags

    def _handle_meta(self, tree):
        """updates meta
        and adds py:content into meta:user-defined and dc:* nodes"""
        root = tree.getroot()
        if root.tag != '{%s}document-meta' % self.namespaces['office']:
            return
        xpath_expr = ("//meta:user-defined[starts-with(., 'relatorio://')]"
                      "|//dc:*[starts-with(., 'relatorio://')]")
        genshi_content = '{%s}content' % self.namespaces['py']
        for node in tree.xpath(xpath_expr, namespaces=self.namespaces):
            node.attrib[genshi_content] = node.text[len('relatorio://'):]

        def set(name, value):
            meta = root.find('{%s}%s' % (self.namespaces['meta'], name))
            if meta is None:
                meta = EtreeElement(
                    '{%s}%s' % (self.namespaces['meta'], name),
                    nsmap=self.namespaces)
                root.append(meta)
            meta.text = value

        def remove(name, namespace='meta'):
            meta = tree.find('{%s}%s' % (self.namespaces[namespace], name))
            if meta is not None:
                tree.remove(meta)

        now = datetime.datetime.now()
        set('creation-date', now.isoformat())
        set('date', now.isoformat())
        remove('document-statistic')
        set('editing-cycles', '1')
        remove('editing-duration')
        set('generator', 'relatorio/%s' % relatorio.__version__)
        remove('initial-creator')
        remove('print-date')
        remove('printed-by')
        remove('creator', 'dc')
        remove('date', 'dc')

    def _handle_relatorio_tags(self, tree):
        """
        Will treat all relatorio tag (py:if/for/choose/when/otherwise)
        tags
        """
        # Some tag/attribute name constants
        table_namespace = self.namespaces['table']
        table_row_tag = '{%s}table-row' % table_namespace
        table_cell_tag = '{%s}table-cell' % table_namespace
        text_namespace = self.namespaces['text']
        text_style_attributes = [s % text_namespace for s in [
                '{%s}class-names', '{%s}cond-style-name', '{%s}style-name']]

        office_value = '{%s}value' % self.namespaces['office']
        office_valuetype = '{%s}value-type' % self.namespaces['office']
        if 'calcext' in self.namespaces:
            calcext_valuetype = '{%s}value-type' % self.namespaces['calcext']
        else:
            calcext_valuetype = None

        py_replace = '{%s}replace' % GENSHI_URI

        r_statements, closing_tags = self._relatorio_statements(tree)

        for r_node, parsed in r_statements:
            expr, directive, attr, a_val = parsed

            # If the node is a genshi directive statement:
            if directive in GENSHI_CLOSING_DIRECTIVE:
                opening = r_node
                closing = closing_tags[id(r_node)]

                # - we find the nearest common ancestor of the closing and
                #   opening statements
                o_ancestors = [opening]
                c_ancestors = [closing] + list(closing.iterancestors())
                ancestor = None
                for node in opening.iterancestors():
                    try:
                        idx = c_ancestors.index(node)
                        assert c_ancestors[idx] == node
                        # we only need ancestors up to the common one
                        del c_ancestors[idx:]
                        ancestor = node
                        break
                    except ValueError:
                        # c_ancestors.index(node) raise ValueError if node is
                        # not a child of c_ancestors
                        pass
                    o_ancestors.append(node)
                assert ancestor is not None, \
                    "No common ancestor found for opening and closing tag"

                outermost_o_ancestor = o_ancestors[-1]
                outermost_c_ancestor = c_ancestors[-1]

                # handle horizontal repetitions (over columns)
                if directive == "for" and ancestor.tag == table_row_tag:
                    a_val = self._handle_column_loops(parsed, ancestor,
                                                      opening,
                                                      outermost_o_ancestor,
                                                      outermost_c_ancestor)

                # - we create a <py:xxx> node
                if attr is not None:
                    attribs = {attr: a_val}
                else:
                    attribs = {}
                genshi_node = EtreeElement('{%s}%s' % (GENSHI_URI,
                                                       directive),
                                           attrib=attribs,
                                           nsmap={'py': GENSHI_URI})

                # - we move all the nodes between the opening and closing
                #   statements to this new node (append also removes from old
                #   parent)
                # - we replace the opening statement by the <py:xxx> node
                # - we delete the closing statement (and its ancestors)
                wrap_nodes_between(outermost_o_ancestor, outermost_c_ancestor,
                                   genshi_node)
            elif directive:
                # find the first parent with the same tag name as the attribute
                parent = r_node
                namespace, name = attr.split(':')
                attr = '{%s}%s' % (self.namespaces[namespace], name)
                while parent is not None and parent.tag != attr:
                    parent = parent.getparent()
                assert parent is not None, "Parent not found"

                # add the py:attribute to the parent
                py_attr = '{%s}%s' % (GENSHI_URI, directive)
                parent.attrib[py_attr] = a_val

                # remove the directive node
                if parent != r_node:
                    remove_node_keeping_tail(r_node)
            else:
                def has_style(node):
                    return any(attr in node.attrib
                               for attr in text_style_attributes)
                # It's not a genshi statement it's a python expression
                parent = r_node.getparent()
                grand_parent = parent.getparent()
                # Guess type only if it is the only value in the cell
                # and its parent has no style
                if ((
                            grand_parent is None
                            or grand_parent.tag != table_cell_tag)
                        or len(grand_parent) != 1
                        or len(parent) != 1
                        or (parent.text and parent.text.strip())
                        or (r_node.tail and r_node.tail.strip())
                        or has_style(parent)):
                    r_node.attrib[py_replace] = (
                        '__relatorio_escape_invalid_chars(%s)' % expr)
                    continue

                cache_id = id(r_node)
                r_node.attrib[py_replace] = ("__relatorio_get_cache(%s)" %
                                             cache_id)
                # The grand-parent tag is a table cell we should set the
                # correct value and type for this cell.
                dico = ('__relatorio_guess_type('
                        '__relatorio_store_cache(%s, %s))')
                update_py_attrs(grand_parent, dico % (cache_id, expr))
                for attr in [office_value,
                             office_valuetype,
                             calcext_valuetype]:
                    if attr:
                        grand_parent.attrib.pop(attr, None)

    def _handle_column_loops(self, statement, ancestor, opening,
                             outer_o_node, outer_c_node):
        _, directive, attr, a_val = statement

        self.has_col_loop = True

        table_namespace = self.namespaces['table']
        table_col_tag = '{%s}table-column' % table_namespace
        table_num_col_attr = '{%s}number-columns-repeated' % table_namespace

        repeat_tag = '{%s}repeat' % RELATORIO_URI

        # table node (it is not necessarily the direct parent of ancestor)
        table_node = next(
            ancestor.iterancestors('{%s}table' % table_namespace))
        table_name = table_node.attrib['{%s}name' % table_namespace]

        # add counting instructions
        loop_id = id(opening)

        # 1) add reset counter code on the row opening tag
        #    (through a py:attrs attribute).
        # Note that table_name is not needed in the first two
        # operations, but a unique id within the table is required
        # to support nested column repetition
        update_py_attrs(ancestor, "__relatorio_reset_col_count(%d)" % loop_id)

        # 2) add increment code (through a py:attrs attribute) on
        #    the first cell node after the opening (cell node)
        #    ancestor
        enclosed_cell = outer_o_node.getnext()
        assert enclosed_cell.tag == '{%s}table-cell' % table_namespace
        update_py_attrs(enclosed_cell, "__relatorio_inc_col_count(%d)" %
                loop_id)

        # 3) add "store count" code as a py:replace node, as the
        #    last child of the row
        attr_value = "__relatorio_store_col_count(%d, %r)" \
                     % (loop_id, table_name)
        replace_node = EtreeElement('{%s}replace' % GENSHI_URI,
                                    attrib={'value': attr_value},
                                    nsmap={'py': GENSHI_URI})
        ancestor.append(replace_node)

        # find the position in the row of the cells holding the
        # <for> and </for> instructions
        # We use "*" so as to count both normal cells and covered/hidden cells
        position_xpath_expr = 'count(preceding-sibling::*)'
        opening_pos = \
            int(outer_o_node.xpath(position_xpath_expr,
                                   namespaces=self.namespaces))
        closing_pos = \
            int(outer_c_node.xpath(position_xpath_expr,
                                   namespaces=self.namespaces))

        # check whether or not the opening tag spans several rows
        a_val = self._handle_row_spanned_column_loops(
                    statement, outer_o_node, opening_pos, closing_pos)

        # check if this table's headers were already processed
        repeat_node = table_node.find(repeat_tag)
        if repeat_node is not None:
            prev_pos = (int(repeat_node.attrib['opening']),
                        int(repeat_node.attrib['closing']))
            if (opening_pos, closing_pos) != prev_pos:
                raise Exception(
                    'Incoherent column repetition found! '
                    'If a table has several lines with repeated '
                    'columns, the repetition need to be on the '
                    'same columns across all lines.')
        else:
            # compute splits: oo collapses the headers of adjacent
            # columns which use the same style. We need to split
            # any column header which is repeated so many times
            # that it encompasses any of the column headers that
            # we need to repeat
            to_split = []
            idx = 0
            childs = list(table_node.iterchildren(table_col_tag))
            for tag in childs:
                inc = int(tag.attrib.get(table_num_col_attr, 1))
                oldidx = idx
                idx += inc
                if oldidx < opening_pos < idx or \
                   oldidx < closing_pos < idx:
                    to_split.append(tag)

            # split tags
            for tag in to_split:
                tag_pos = table_node.index(tag)
                num = int(tag.attrib.pop(table_num_col_attr))
                new_tags = [deepcopy(tag) for i in range(num)]
                table_node[tag_pos:tag_pos + 1] = new_tags

            # compute the column header nodes corresponding to
            # the opening and closing tags.
            first = table_node[opening_pos]
            last = table_node[closing_pos]

            # add a <relatorio:repeat> node around the column
            # definitions nodes
            attribs = {
                "opening": str(opening_pos),
                "closing": str(closing_pos),
                "table": table_name,
                }
            repeat_node = EtreeElement(repeat_tag, attrib=attribs,
                                       nsmap={'relatorio': RELATORIO_URI})
            wrap_nodes_between(first, last, repeat_node)
        return a_val

    def _handle_row_spanned_column_loops(self, statement, outer_o_node,
                                         opening_pos, closing_pos):
        """handles column repetitions which span several rows, by duplicating
        the py:for node for each row, and make the loops work on a copy of the
        original iterable as to not exhaust generators."""

        _, directive, attr, a_val = statement
        table_namespace = self.namespaces['table']
        table_rowspan_attr = '{%s}number-rows-spanned' % table_namespace

        # checks wether there is a (meaningful) rowspan
        rows_spanned = int(outer_o_node.attrib.get(table_rowspan_attr, 1))
        if rows_spanned == 1:
            return a_val

        table_row_tag = '{%s}table-row' % table_namespace
        table_cov_cell_tag = '{%s}covered-table-cell' % table_namespace

        # if so, we need to:

        # 1) create a with node to define a temporary variable
        temp_var = "__relatorio_temp%d" % id(outer_o_node)
        # a_val == "target in iterable"
        target, iterable = a_val.split(' in ', 1)
        vars = "%s = list(%s)" % (temp_var, iterable.strip())
        with_node = EtreeElement('{%s}with' % GENSHI_URI,
                                 attrib={"vars": vars},
                                 nsmap={'py': GENSHI_URI})

        # 2) transform a_val to use that temporary variable
        a_val = "%s in %s" % (target, temp_var)

        # 3) wrap the corresponding cells on the next row(s)
        #    (those should be covered-table-cell) inside a
        #    duplicate py:for node (looping on the temporary
        #    variable).
        row_node = outer_o_node.getparent()
        row_node.addprevious(with_node)
        rows_to_wrap = [row_node]
        assert row_node.tag == table_row_tag
        next_rows = row_node.itersiblings(table_row_tag)
        for row_idx in range(rows_spanned - 1):
            next_row_node = next(next_rows)
            rows_to_wrap.append(next_row_node)
            # compute the start and end nodes
            first = next_row_node[opening_pos]
            last = next_row_node[closing_pos]
            assert first.tag == table_cov_cell_tag
            assert last.tag == table_cov_cell_tag
            # wrap them
            tag = '{%s}%s' % (GENSHI_URI, directive)
            for_node = EtreeElement(tag,
                                    attrib={attr: a_val},
                                    nsmap={'py': GENSHI_URI})
            wrap_nodes_between(first, last, for_node)

        # 4) wrap all the corresponding rows indide the "with"
        #    node
        for node in rows_to_wrap:
            with_node.append(node)
        return a_val

    def _handle_images(self, tree):
        "replaces all draw:frame named 'image: ...' by draw:image nodes"
        draw_namespace = self.namespaces['draw']
        draw_name = '{%s}name' % draw_namespace
        draw_image = '{%s}image' % draw_namespace
        py_attrs = '{%s}attrs' % self.namespaces['py']
        end_cell_address = '{%s}end-cell-address' % self.namespaces['table']
        svg_namespace = self.namespaces['svg']
        svg_width = '{%s}width' % svg_namespace
        svg_height = '{%s}height' % svg_namespace
        xpath_expr = "//draw:frame[starts-with(@draw:name, 'image:')]"
        for draw in tree.xpath(xpath_expr, namespaces=self.namespaces):
            cache_id = id(draw)
            d_name = draw.attrib[draw_name][6:].strip()
            attr_expr = ("__relatorio_make_href(__relatorio_get_cache(%s))" %
                         cache_id)
            image_node = EtreeElement(draw_image,
                                      attrib={py_attrs: attr_expr},
                                      nsmap={'draw': draw_namespace,
                                             'py': GENSHI_URI})
            draw.replace(draw[0], image_node)
            width = draw.attrib.pop(svg_width, '')
            height = draw.attrib.pop(svg_height, '')
            attr_expr = ("__relatorio_make_dimension("
                         "__relatorio_store_cache(%s, (%s)), '%s', '%s')" %
                         (cache_id, d_name, width, height))
            draw.attrib[py_attrs] = attr_expr
            draw.attrib.pop(draw_name)
            dico = "{'%s': (__relatorio_get_cache(%s)[4:5] or [''])[0]}"
            update_py_attrs(draw, dico % (draw_name, cache_id))
            # remove end-cell-address as the address specified could be wrong
            draw.attrib.pop(end_cell_address, '')

    def _handle_innerdocs(self, tree):
        "finds inner_docs and adds them to the processing stack."
        href_attrib = '{%s}href' % self.namespaces['xlink']
        xpath_expr = "//draw:object[starts-with(@xlink:href, './')" \
                     "and @xlink:show='embed']"
        for draw in tree.xpath(xpath_expr, namespaces=self.namespaces):
            self.inner_docs.append(draw.attrib[href_attrib][2:])

    def _escape_values(self, tree):
        "escapes element values"
        for element in tree.iter():
            for attrs in list(element.keys()):
                if not attrs.startswith('{%s}' % GENSHI_URI):
                    element.attrib[attrs] = element.attrib[attrs]\
                            .replace(PREFIX, PREFIX * 2)
            if element.text:
                element.text = element.text.replace(PREFIX, PREFIX * 2)

    def _guess_type(self, val):
        office_namespace = self.namespaces['office']
        types = {'boolean': '{%s}boolean-value' % office_namespace,
                 'currency': '{%s}currency' % office_namespace,
                 'date': '{%s}date-value' % office_namespace,
                 'float': '{%s}value' % office_namespace,
                 'percentage': '{%s}value' % office_namespace,
                 'string': '{%s}string-value' % office_namespace,
                 'time': '{%s}time-value' % office_namespace,
                 'void': '{%s}value' % office_namespace,
                 }
        attrs = dict.fromkeys(types)
        # Missing base type for currency and percentage
        if isinstance(val, bool):
            type_ = 'boolean'
            val = str(val).lower()
        elif isinstance(val, datetime.date):
            type_ = 'date'
            val = val.isoformat()
        elif isinstance(val, (int, float, Decimal)):
            type_ = 'float'
        elif isinstance(val, str):
            type_ = 'string'
            val = escape_xml_invalid_chars(val)
        elif isinstance(val, datetime.timedelta):
            type_ = 'time'
            val = 'P%sD%sS' % (val.days, val.seconds)
        else:
            type_ = 'void'
            val = None
        attrs[types[type_]] = val
        attrs['{%s}value-type' % office_namespace] = type_
        if 'calcext' in self.namespaces:
            attrs['{%s}value-type' % self.namespaces['calcext']] = type_
        return attrs

    def generate(self, *args,
            _relatorio_compresslevel=None,
            _relatorio_chunksize=64,
            _relatorio_zip64=False,
            _relatorio_compression_method=zipfile.ZIP_DEFLATED,
            **kwargs):
        "creates the RelatorioStream."
        serializer = OOSerializer(
            self._source, self._files,
            compresslevel=_relatorio_compresslevel,
            zip64=_relatorio_zip64,
            chunksize=_relatorio_chunksize,
            compression_method=_relatorio_compression_method)
        kwargs['__relatorio_make_href'] = ImageHref(serializer, kwargs)
        kwargs['__relatorio_make_dimension'] = ImageDimension(self.namespaces)
        kwargs['__relatorio_guess_type'] = self._guess_type
        kwargs['__relatorio_escape_invalid_chars'] = escape_xml_invalid_chars

        counter = ColumnCounter()
        kwargs['__relatorio_reset_col_count'] = counter.reset
        kwargs['__relatorio_inc_col_count'] = counter.inc
        kwargs['__relatorio_store_col_count'] = counter.store

        cache = ExpressionCache()
        kwargs['__relatorio_store_cache'] = cache.store
        kwargs['__relatorio_get_cache'] = cache.get

        stream = super(Template, self).generate(*args, **kwargs)
        if self.has_col_loop:
            # Note that we can't simply add a "number-columns-repeated"
            # attribute and then fill it with the correct number of columns
            # because that wouldn't work if more than one column is repeated.
            transformation = DuplicateColumnHeaders(counter)
            col_filter = Transformer('//repeat[namespace-uri()="%s"]'
                                     % RELATORIO_URI)
            col_filter = col_filter.apply(transformation)
            # Must consume the stream to fill counter
            stream = Stream(list(stream), self.serializer) | col_filter
        return RelatorioStream(stream, serializer)


class DuplicateColumnHeaders(object):
    def __init__(self, counter):
        self.counter = counter

    def __call__(self, stream):
        for mark, (kind, data, pos) in stream:
            # for each repeat tag found
            if mark is ENTER:
                # get the number of columns for that table
                attrs = data[1]
                table = attrs.get('table')
                col_count = self.counter.counters[table]

                # collect events (column header tags) to repeat
                events = []
                for submark, event in stream:
                    if submark is EXIT:
                        break
                    events.append(event)

                # repeat them
                for _ in range(col_count):
                    for event in events:
                        yield None, event
            else:
                yield mark, (kind, data, pos)


def get_zip_file(source):
    try:
        return zipfile.ZipFile(source)
    except zipfile.BadZipfile:
        # ZipFile modify the position
        if hasattr(source, 'seek'):
            source.seek(0)
        return zipfile.ZipFile(fod2od(source))


def fod2od(source):
    "Convert Flat OpenDocument to OpenDocument"
    odt_io = BytesIO()
    odt_zip = zipfile.ZipFile(
        odt_io, mode='w', compression=zipfile.ZIP_DEFLATED)
    fodt_tree = lxml.etree.parse(source)
    fodt_root = fodt_tree.getroot()
    office_ns = fodt_root.nsmap['office']
    tag2files = {
        '{%s}meta' % office_ns: ['meta'],
        '{%s}settings' % office_ns: ['settings'],
        '{%s}scripts' % office_ns: ['content'],
        '{%s}font-face-decls' % office_ns: ['content', 'styles'],
        '{%s}styles' % office_ns: ['styles'],
        '{%s}automatic-styles' % office_ns: ['content', 'styles'],
        '{%s}master-styles' % office_ns: ['styles'],
        '{%s}body' % office_ns: ['content'],
        }
    mimetype = fodt_root.attrib['{%s}mimetype' % office_ns]
    # mimetype should be written first to let detection through 'magic number'
    odt_zip.writestr('mimetype', mimetype, zipfile.ZIP_STORED)
    documents = {}
    images = []
    for child in fodt_root:
        for fname in tag2files[child.tag]:
            document = documents.get(fname)
            if document is None:
                document = lxml.etree.Element(
                    '{%s}document-%s' % (office_ns, fname),
                    nsmap=fodt_root.nsmap)
                documents[fname] = document
            child = deepcopy(child)
            images.extend(extract_images(
                    child, fodt_root.nsmap, start=len(images)))
            document.append(child)
    manifest = Manifest(b'''<?xml version="1.0" encoding="UTF-8"?>
        <manifest:manifest
        xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>
        ''')
    manifest.add_file_entry('/', mimetype)
    for fname, document in documents.items():
        document_string = lxml.etree.tostring(document, encoding='UTF-8',
                                              xml_declaration=True)
        odt_zip.writestr('%s.xml' % fname, document_string)
        manifest.add_file_entry('%s.xml' % fname, 'text/xml')
    for fname, data, mime_type in images:
        odt_zip.writestr(fname, data)
        manifest.add_file_entry(fname, mime_type)
    odt_zip.writestr(MANIFEST, str(manifest))
    odt_zip.close()
    return odt_io


def extract_images(child, namespaces, start=0):
    "Extract draw:image with binary-data and replace by href"
    import magic
    images = []
    for i, image in enumerate(
            child.xpath('//draw:image', namespaces=namespaces), start):
        binary_data, = image.xpath(
            './office:binary-data', namespaces=namespaces)
        data = base64.b64decode(binary_data.text)
        if hasattr(magic, 'from_buffer'):
            mime_type = magic.from_buffer(data, mime=True)
        else:
            # Not python-magic but file-magic
            mime_type = magic.detect_from_content(data).mime_type
        name = 'Pictures/image%s%s' % (
            i, mimetypes.guess_extension(mime_type))
        image.remove(binary_data)
        xlink_ns = namespaces['xlink']
        image.attrib['{%s}href' % xlink_ns] = name
        images.append((name, data, mime_type))
    return images


class Manifest(object):

    def __init__(self, content):
        self.tree = lxml.etree.parse(BytesIO(content))
        self.root = self.tree.getroot()
        self.namespaces = self.root.nsmap

    def __str__(self):
        val = lxml.etree.tostring(self.tree, encoding='UTF-8',
                                  xml_declaration=True)
        # In Python 3, val will be bytes
        if not isinstance(val, str):
            return str(val, 'utf-8')
        return val

    def add_file_entry(self, path, mimetype=None):
        manifest_namespace = self.namespaces['manifest']
        attribs = {'{%s}media-type' % manifest_namespace: mimetype or '',
                   '{%s}full-path' % manifest_namespace: path}
        entry_node = EtreeElement('{%s}%s' % (manifest_namespace,
                                              'file-entry'),
                                  attrib=attribs,
                                  nsmap={'manifest': manifest_namespace})
        self.root.append(entry_node)

    def remove_file_entry(self, path):
        manifest_namespace = self.namespaces['manifest']
        entry = self.root.find('{%s}%s[@{%s}full-path="%s"]' %
                               (manifest_namespace, 'file-entry',
                                manifest_namespace, path))
        if entry is not None:
            self.root.remove(entry)


class _AbstractZipWriteSplitStream(object):
    def __init__(self, zipfile, chunksize=64, zip64=False):
        self.zipfile = zipfile
        self.chunksize = chunksize
        self.zip64 = zip64

    def open(self, zinfo):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def __call__(self, stream):
        for kind, data, pos in stream:
            if kind == genshi.core.PI and data[0] == 'relatorio':
                self.open(data[1])
                continue
            yield kind, data, pos
        self.close()

    def write(self, data):
        raise NotImplementedError


if sys.version_info >= (3, 6):
    class _ZipWriteSplitStream(_AbstractZipWriteSplitStream):
        def __init__(self, *args, **kwargs):
            super(_ZipWriteSplitStream, self).__init__(*args, **kwargs)
            self._fp = None
            self._buffer = []
            self._zinfo = None

        def open(self, zinfo):
            if self._fp or self._buffer:
                self.close()
            self._zinfo = zinfo
            self._fp = None

        def close(self):
            self.flush()
            self._fp.close()
            self._zinfo = None
            self._fp = None

        def write(self, data):
            self._buffer.append(data)
            if len(self._buffer) > self.chunksize:
                self.flush()

        def flush(self):
            if not self._fp:
                self._fp = self.zipfile.open(
                    self._zinfo, mode='w', force_zip64=self.zip64)
            self._fp.write(b''.join(self._buffer))
            self._buffer.clear()
else:
    class _ZipWriteSplitStream(_AbstractZipWriteSplitStream):
        def __init__(self, *args, **kwargs):
            super(_ZipWriteSplitStream, self).__init__(*args, **kwargs)
            self._fp = None
            self._zinfo = None

        def open(self, zinfo):
            if self._fp:
                self.close()
            self._zinfo = zinfo
            self._fp = BytesIO()

        def close(self):
            self._fp.seek(0)
            self.zipfile.writestr(self._zinfo, self._fp.read())
            self._zinfo = None
            self._fp = None

        def write(self, data):
            self._fp.write(data)


class OOSerializer:

    def __init__(self, source, files, chunksize=64,
            compresslevel=None, zip64=False,
            compression_method=zipfile.ZIP_DEFLATED):
        self.inzip = get_zip_file(source)
        self.manifest = Manifest(self.inzip.read(MANIFEST))
        self.xml_serializer = genshi.output.XMLSerializer()
        self._files = files
        self.chunksize = chunksize
        self.compresslevel = None
        self.zip64 = zip64
        self.compression_method = compression_method
        self.outzip = None
        self._deferred = []

    def __call__(self, stream, method=None, encoding='utf-8', out=None):
        if out is None:
            result = BytesIO()
        else:
            result = out
        zip_options = {}
        if sys.version_info >= (3, 7):
            zip_options['compresslevel'] = self.compresslevel
        self.outzip = zipfile.ZipFile(
            result, mode='w', compression=self.compression_method,
            **zip_options)
        files = {}
        now = time.localtime()[:6]
        manifest_info = None
        for f_info in self.inzip.infolist():
            if f_info.filename.startswith('ObjectReplacements'):
                continue
            elif f_info.filename in self._files:
                # create a new file descriptor, copying some attributes from
                # the original file
                new_info = zipfile.ZipInfo(f_info.filename, now)
                for attr in ('compress_type', 'flag_bits', 'create_system'):
                    setattr(new_info, attr, getattr(f_info, attr))
                files[f_info.filename] = new_info
            elif f_info.filename == MANIFEST:
                manifest_info = f_info
            elif f_info.filename.startswith(THUMBNAILS + '/'):
                self.manifest.remove_file_entry(f_info.filename)
            else:
                self.outzip.writestr(f_info, self.inzip.read(f_info.filename))

        writer = _ZipWriteSplitStream(self.outzip, self.chunksize, self.zip64)
        output_encode(
            self.xml_serializer(writer(stream)), encoding=encoding, out=writer)

        for args in self._deferred:
            self.add_file(*args)
        self.manifest.remove_file_entry(THUMBNAILS + '/')
        if manifest_info:
            self.outzip.writestr(manifest_info, str(self.manifest))
        self.inzip.close()
        self.outzip.close()

        if out is None:
            return result

    def add_file(self, path, content, mimetype):
        if not self.outzip:
            self._deferred.append((path, content, mimetype))
        elif path not in self.outzip.namelist():
            try:
                self.outzip.writestr(path, content)
                self.manifest.add_file_entry(path, mimetype)
            except ValueError:
                self._deferred.append((path, content, mimetype))


MIMETemplateLoader.add_factory('oo.org', Template)
