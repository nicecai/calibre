#!/usr/bin/env python


__copyright__ = '2008, Kovid Goyal kovid@kovidgoyal.net'
__docformat__ = 'restructuredtext en'
__license__   = 'GPL v3'

import json, os, traceback

from qt.core import (Qt, QDialog, QDialogButtonBox, QSyntaxHighlighter, QFont,
                      QRegExp, QApplication, QTextCharFormat, QColor, QCursor,
                      QIcon, QSize, QPalette, QLineEdit, QByteArray,
                      QFontInfo, QFontDatabase)

from calibre import sanitize_file_name
from calibre.constants import config_dir
from calibre.gui2 import gprefs
from calibre.gui2.dialogs.template_dialog_ui import Ui_TemplateDialog
from calibre.utils.formatter_functions import formatter_functions
from calibre.utils.icu import sort_key
from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.book.formatter import SafeFormat
from calibre.library.coloring import (displayable_columns, color_row_key)
from calibre.gui2 import error_dialog, choose_files, pixmap_to_data
from calibre.utils.localization import localize_user_manual_link
from polyglot.builtins import unicode_type


class ParenPosition:

    def __init__(self, block, pos, paren):
        self.block = block
        self.pos = pos
        self.paren = paren
        self.highlight = False

    def set_highlight(self, to_what):
        self.highlight = to_what


class TemplateHighlighter(QSyntaxHighlighter):

    Config = {}
    Rules = []
    Formats = {}
    BN_FACTOR = 1000

    KEYWORDS = ["program", 'if', 'then', 'else', 'elif', 'fi']

    def __init__(self, parent=None, builtin_functions=None):
        super(TemplateHighlighter, self).__init__(parent)

        self.initializeFormats()

        TemplateHighlighter.Rules.append((QRegExp(
                "|".join([r"\b%s\b" % keyword for keyword in self.KEYWORDS])),
                "keyword"))
        TemplateHighlighter.Rules.append((QRegExp(
                "|".join([r"\b%s\b" % builtin for builtin in
                          (builtin_functions if builtin_functions else
                                                formatter_functions().get_builtins())])),
                "builtin"))

        TemplateHighlighter.Rules.append((QRegExp(
                r"\b[+-]?[0-9]+[lL]?\b"
                r"|\b[+-]?0[xX][0-9A-Fa-f]+[lL]?\b"
                r"|\b[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\b"),
                "number"))

        stringRe = QRegExp(r"""(?:[^:]'[^']*'|"[^"]*")""")
        stringRe.setMinimal(True)
        TemplateHighlighter.Rules.append((stringRe, "string"))

        lparenRe = QRegExp(r'\(')
        lparenRe.setMinimal(True)
        TemplateHighlighter.Rules.append((lparenRe, "lparen"))
        rparenRe = QRegExp(r'\)')
        rparenRe.setMinimal(True)
        TemplateHighlighter.Rules.append((rparenRe, "rparen"))

        self.regenerate_paren_positions()
        self.highlighted_paren = False

    def initializeFormats(self):
        font = gprefs.get('gpm_template_editor_font', 'monospace')
        Config = self.Config
        Config["fontfamily"] = font
        pal = QApplication.instance().palette()
        for name, color, bold, italic in (
                ("normal", None, False, False),
                ("keyword", pal.color(QPalette.ColorRole.Link).name(), True, False),
                ("builtin", pal.color(QPalette.ColorRole.Link).name(), False, False),
                ("comment", "#007F00", False, True),
                ("string", "#808000", False, False),
                ("number", "#924900", False, False),
                ("lparen", None, True, True),
                ("rparen", None, True, True)):
            Config["%sfontcolor" % name] = color
            Config["%sfontbold" % name] = bold
            Config["%sfontitalic" % name] = italic
        baseFormat = QTextCharFormat()
        baseFormat.setFontFamily(Config["fontfamily"])
        Config["fontsize"] = gprefs['gpm_template_editor_font_size']
        baseFormat.setFontPointSize(Config["fontsize"])

        for name in ("normal", "keyword", "builtin", "comment",
                     "string", "number", "lparen", "rparen"):
            format = QTextCharFormat(baseFormat)
            col = Config["%sfontcolor" % name]
            if col:
                format.setForeground(QColor(col))
            if Config["%sfontbold" % name]:
                format.setFontWeight(QFont.Weight.Bold)
            format.setFontItalic(Config["%sfontitalic" % name])
            self.Formats[name] = format

    def find_paren(self, bn, pos):
        dex = bn * self.BN_FACTOR + pos
        return self.paren_pos_map.get(dex, None)

    def highlightBlock(self, text):
        bn = self.currentBlock().blockNumber()
        textLength = len(text)

        self.setFormat(0, textLength, self.Formats["normal"])

        if not text:
            pass
        elif text[0] == u"#":
            self.setFormat(0, textLength, self.Formats["comment"])
            return

        for regex, format in TemplateHighlighter.Rules:
            i = regex.indexIn(text)
            while i >= 0:
                length = regex.matchedLength()
                if format in ['lparen', 'rparen']:
                    pp = self.find_paren(bn, i)
                    if pp and pp.highlight:
                        self.setFormat(i, length, self.Formats[format])
                else:
                    self.setFormat(i, length, self.Formats[format])
                i = regex.indexIn(text, i + length)

        if self.generate_paren_positions:
            t = unicode_type(text)
            i = 0
            foundQuote = False
            while i < len(t):
                c = t[i]
                if c == ':':
                    # Deal with the funky syntax of template program mode.
                    # This won't work if there are more than one template
                    # expression in the document.
                    if not foundQuote and i+1 < len(t) and t[i+1] == "'":
                        i += 2
                elif c in ["'", '"']:
                    foundQuote = True
                    i += 1
                    j = t[i:].find(c)
                    if j < 0:
                        i = len(t)
                    else:
                        i = i + j
                elif c in ['(', ')']:
                    pp = ParenPosition(bn, i, c)
                    self.paren_positions.append(pp)
                    self.paren_pos_map[bn*self.BN_FACTOR+i] = pp
                i += 1

    def rehighlight(self):
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        QSyntaxHighlighter.rehighlight(self)
        QApplication.restoreOverrideCursor()

    def check_cursor_pos(self, chr, block, pos_in_block):
        found_pp = -1
        for i, pp in enumerate(self.paren_positions):
            pp.set_highlight(False)
            if pp.block == block and pp.pos == pos_in_block:
                found_pp = i

        if chr not in ['(', ')']:
            if self.highlighted_paren:
                self.rehighlight()
                self.highlighted_paren = False
            return

        if found_pp >= 0:
            stack = 0
            if chr == '(':
                list = self.paren_positions[found_pp+1:]
            else:
                list = reversed(self.paren_positions[0:found_pp])
            for pp in list:
                if pp.paren == chr:
                    stack += 1
                elif stack:
                    stack -= 1
                else:
                    pp.set_highlight(True)
                    self.paren_positions[found_pp].set_highlight(True)
                    break
        self.highlighted_paren = True
        self.rehighlight()

    def regenerate_paren_positions(self):
        self.generate_paren_positions = True
        self.paren_positions = []
        self.paren_pos_map = {}
        self.rehighlight()
        self.generate_paren_positions = False


class TemplateDialog(QDialog, Ui_TemplateDialog):

    def __init__(self, parent, text, mi=None, fm=None, color_field=None,
                 icon_field_key=None, icon_rule_kind=None, doing_emblem=False,
                 text_is_placeholder=False, dialog_is_st_editor=False,
                 global_vars=None, all_functions=None, builtin_functions=None):
        QDialog.__init__(self, parent)
        Ui_TemplateDialog.__init__(self)
        self.setupUi(self)

        self.coloring = color_field is not None
        self.iconing = icon_field_key is not None
        self.embleming = doing_emblem
        self.dialog_is_st_editor = dialog_is_st_editor
        if global_vars is None:
            self.global_vars = {}
        else:
            self.global_vars = global_vars

        cols = []
        if fm is not None:
            for key in sorted(displayable_columns(fm),
                              key=lambda k: sort_key(fm[k]['name'] if k != color_row_key else 0)):
                if key == color_row_key and not self.coloring:
                    continue
                from calibre.gui2.preferences.coloring import all_columns_string
                name = all_columns_string if key == color_row_key else fm[key]['name']
                if name:
                    cols.append((name, key))

        self.color_layout.setVisible(False)
        self.icon_layout.setVisible(False)

        if self.coloring:
            self.color_layout.setVisible(True)
            for n1, k1 in cols:
                self.colored_field.addItem(n1 +
                       (' (' + k1 + ')' if k1 != color_row_key else ''), k1)
            self.colored_field.setCurrentIndex(self.colored_field.findData(color_field))
        elif self.iconing or self.embleming:
            self.icon_layout.setVisible(True)
            if self.embleming:
                self.icon_kind_label.setVisible(False)
                self.icon_kind.setVisible(False)
                self.icon_chooser_label.setVisible(False)
                self.icon_field.setVisible(False)

            for n1, k1 in cols:
                self.icon_field.addItem('{} ({})'.format(n1, k1), k1)
            self.icon_file_names = []
            d = os.path.join(config_dir, 'cc_icons')
            if os.path.exists(d):
                for icon_file in os.listdir(d):
                    icon_file = icu_lower(icon_file)
                    if os.path.exists(os.path.join(d, icon_file)):
                        if icon_file.endswith('.png'):
                            self.icon_file_names.append(icon_file)
            self.icon_file_names.sort(key=sort_key)
            self.update_filename_box()

            if self.iconing:
                dex = 0
                from calibre.gui2.preferences.coloring import icon_rule_kinds
                for i,tup in enumerate(icon_rule_kinds):
                    txt,val = tup
                    self.icon_kind.addItem(txt, userData=(val))
                    if val == icon_rule_kind:
                        dex = i
                self.icon_kind.setCurrentIndex(dex)
                self.icon_field.setCurrentIndex(self.icon_field.findData(icon_field_key))

        if dialog_is_st_editor:
            self.buttonBox.setVisible(False)
        else:
            self.new_doc_label.setVisible(False)
            self.new_doc.setVisible(False)
            self.template_name_label.setVisible(False)
            self.template_name.setVisible(False)

        if mi:
            if not isinstance(mi, list):
                mi = (mi, )
        else:
            mi = Metadata(_('Title'), [_('Author')])
            mi.author_sort = _('Author Sort')
            mi.series = ngettext('Series', 'Series', 1)
            mi.series_index = 3
            mi.rating = 4.0
            mi.tags = [_('Tag 1'), _('Tag 2')]
            mi.languages = ['eng']
            mi.id = 1
            if fm is not None:
                self.mi.set_all_user_metadata(fm.custom_field_metadata())
            else:
                # No field metadata. Grab a copy from the current library so
                # that we can validate any custom column names. The values for
                # the columns will all be empty, which in some very unusual
                # cases might cause formatter errors. We can live with that.
                from calibre.gui2.ui import get_gui
                mi.set_all_user_metadata(
                      get_gui().current_db.new_api.field_metadata.custom_field_metadata())
            for col in mi.get_all_user_metadata(False):
                mi.set(col, (col,), 0)
            mi = (mi, )
        self.mi = mi

        # Set up the display table
        self.table_column_widths = None
        try:
            self.table_column_widths = \
                        gprefs.get('template_editor_table_widths', None)
        except:
            pass
        tv = self.template_value
        tv.setRowCount(len(mi))
        tv.setColumnCount(2)
        tv.setHorizontalHeaderLabels((_('Book title'), _('Template value')))
        tv.horizontalHeader().setStretchLastSection(True)
        tv.horizontalHeader().sectionResized.connect(self.table_column_resized)
        # Set the height of the table
        h = tv.rowHeight(0) * min(len(mi), 5)
        h += 2 * tv.frameWidth() + tv.horizontalHeader().height()
        tv.setMinimumHeight(h)
        tv.setMaximumHeight(h)
        # Set the size of the title column
        if self.table_column_widths:
            tv.setColumnWidth(0, self.table_column_widths[0])
        else:
            tv.setColumnWidth(0, tv.fontMetrics().averageCharWidth() * 10)
        # Use our own widget to get rid of elision. setTextElideMode() doesn't work
        for r in range(0, len(mi)):
            w = QLineEdit(tv)
            w.setReadOnly(True)
            tv.setCellWidget(r, 0, w)
            w = QLineEdit(tv)
            w.setReadOnly(True)
            tv.setCellWidget(r, 1, w)

        # Remove help icon on title bar
        icon = self.windowIcon()
        self.setWindowFlags(self.windowFlags()&(~Qt.WindowType.WindowContextHelpButtonHint))
        self.setWindowIcon(icon)

        self.all_functions = all_functions if all_functions else formatter_functions().get_functions()
        self.builtins = (builtin_functions if builtin_functions else
                         formatter_functions().get_builtins_and_aliases())

        self.last_text = ''
        self.highlighter = TemplateHighlighter(self.textbox.document(), builtin_functions=self.builtins)
        self.textbox.cursorPositionChanged.connect(self.text_cursor_changed)
        self.textbox.textChanged.connect(self.textbox_changed)

        self.textbox.setTabStopWidth(10)
        self.source_code.setTabStopWidth(10)
        self.documentation.setReadOnly(True)
        self.source_code.setReadOnly(True)

        if text is not None:
            if text_is_placeholder:
                self.textbox.setPlaceholderText(text)
                self.textbox.clear()
                text = ''
            else:
                self.textbox.setPlainText(text)
        else:
            text = ''
        self.buttonBox.button(QDialogButtonBox.StandardButton.Ok).setText(_('&OK'))
        self.buttonBox.button(QDialogButtonBox.StandardButton.Cancel).setText(_('&Cancel'))
        self.color_copy_button.clicked.connect(self.color_to_clipboard)
        self.filename_button.clicked.connect(self.filename_button_clicked)
        self.icon_copy_button.clicked.connect(self.icon_to_clipboard)

        try:
            with open(P('template-functions.json'), 'rb') as f:
                self.builtin_source_dict = json.load(f, encoding='utf-8')
        except:
            self.builtin_source_dict = {}

        func_names = sorted(self.all_functions)
        self.function.clear()
        self.function.addItem('')
        for f in func_names:
            self.function.addItem('{}  --  {}'.format(f,
                               self.function_type_string(f, longform=False)), f)
        self.function.setCurrentIndex(0)
        self.function.currentIndexChanged.connect(self.function_changed)
        self.display_values(text)
        self.rule = (None, '')

        tt = _('Template language tutorial')
        self.template_tutorial.setText(
            '<a href="%s">%s</a>' % (
                localize_user_manual_link('https://manual.calibre-ebook.com/template_lang.html'), tt))
        tt = _('Template function reference')
        self.template_func_reference.setText(
            '<a href="%s">%s</a>' % (
                localize_user_manual_link('https://manual.calibre-ebook.com/generated/en/template_ref.html'), tt))

        self.set_up_font_boxes()
        self.textbox.setFocus()
        # Now geometry
        try:
            geom = gprefs.get('template_editor_dialog_geometry', None)
            if geom is not None:
                QApplication.instance().safe_restore_geometry(self, QByteArray(geom))
        except Exception:
            pass

    def set_up_font_boxes(self):
        family = gprefs.get('gpm_template_editor_font', 'monospace')
        size = gprefs['gpm_template_editor_font_size']
        font = QFont(family, pointSize=size)
        self.font_box.setWritingSystem(QFontDatabase.Latin)
        self.font_box.setCurrentFont(font)
        self.font_box.setEditable(False)
        self.font_box.currentFontChanged.connect(self.font_changed)
        self.font_size_box.setValue(gprefs['gpm_template_editor_font_size'])
        self.font_size_box.valueChanged.connect(self.font_size_changed)

    def font_changed(self, font):
        fi = QFontInfo(font)
        gprefs['gpm_template_editor_font_size'] = fi.pointSize()
        gprefs['gpm_template_editor_font'] = unicode_type(fi.family())
        self.highlighter.initializeFormats()
        self.highlighter.rehighlight()

    def font_size_changed(self, toWhat):
        gprefs['gpm_template_editor_font_size'] = toWhat
        self.highlighter.initializeFormats()
        self.highlighter.rehighlight()

    def filename_button_clicked(self):
        try:
            path = choose_files(self, 'choose_category_icon',
                        _('Select icon'), filters=[
                        ('Images', ['png', 'gif', 'jpg', 'jpeg'])],
                    all_files=False, select_only_single_file=True)
            if path:
                icon_path = path[0]
                icon_name = sanitize_file_name(
                             os.path.splitext(
                                   os.path.basename(icon_path))[0]+'.png')
                if icon_name not in self.icon_file_names:
                    self.icon_file_names.append(icon_name)
                    self.update_filename_box()
                    try:
                        p = QIcon(icon_path).pixmap(QSize(128, 128))
                        d = os.path.join(config_dir, 'cc_icons')
                        if not os.path.exists(os.path.join(d, icon_name)):
                            if not os.path.exists(d):
                                os.makedirs(d)
                            with open(os.path.join(d, icon_name), 'wb') as f:
                                f.write(pixmap_to_data(p, format='PNG'))
                    except:
                        traceback.print_exc()
                self.icon_files.setCurrentIndex(self.icon_files.findText(icon_name))
                self.icon_files.adjustSize()
        except:
            traceback.print_exc()
        return

    def update_filename_box(self):
        self.icon_files.clear()
        self.icon_file_names.sort(key=sort_key)
        self.icon_files.addItem('')
        self.icon_files.addItems(self.icon_file_names)
        for i,filename in enumerate(self.icon_file_names):
            icon = QIcon(os.path.join(config_dir, 'cc_icons', filename))
            self.icon_files.setItemIcon(i+1, icon)

    def color_to_clipboard(self):
        app = QApplication.instance()
        c = app.clipboard()
        c.setText(unicode_type(self.color_name.color))

    def icon_to_clipboard(self):
        app = QApplication.instance()
        c = app.clipboard()
        c.setText(unicode_type(self.icon_files.currentText()))

    def textbox_changed(self):
        cur_text = unicode_type(self.textbox.toPlainText())
        if self.last_text != cur_text:
            self.last_text = cur_text
            self.highlighter.regenerate_paren_positions()
            self.text_cursor_changed()
            self.display_values(cur_text)

    def display_values(self, txt):
        tv = self.template_value
        for r,mi in enumerate(self.mi):
            w = tv.cellWidget(r, 0)
            w.setText(mi.title)
            w.setCursorPosition(0)
            v = SafeFormat().safe_format(txt, mi, _('EXCEPTION: '),
                                         mi, global_vars=self.global_vars,
                                         template_functions=self.all_functions)
            w = tv.cellWidget(r, 1)
            w.setText(v)
            w.setCursorPosition(0)

    def text_cursor_changed(self):
        cursor = self.textbox.textCursor()
        position = cursor.position()
        t = unicode_type(self.textbox.toPlainText())
        if position > 0 and position <= len(t):
            block_number = cursor.blockNumber()
            pos_in_block = cursor.positionInBlock() - 1
            self.highlighter.check_cursor_pos(t[position-1], block_number,
                                              pos_in_block)

    def function_type_string(self, name, longform=True):
        if self.all_functions[name].is_python:
            if name in self.builtins:
                return (_('Built-in template function') if longform else
                            _('Built-in function'))
            return (_('User defined Python template function') if longform else
                            _('User function'))
        else:
            return (_('Stored user defined template') if longform else _('Stored template'))

    def function_changed(self, toWhat):
        name = unicode_type(self.function.itemData(toWhat))
        self.source_code.clear()
        self.documentation.clear()
        self.func_type.clear()
        if name in self.all_functions:
            self.documentation.setPlainText(self.all_functions[name].doc)
            if name in self.builtins and name in self.builtin_source_dict:
                self.source_code.setPlainText(self.builtin_source_dict[name])
            else:
                self.source_code.setPlainText(self.all_functions[name].program_text)
            self.func_type.setText(self.function_type_string(name, longform=True))

    def table_column_resized(self, col, old, new):
        self.table_column_widths = []
        for c in range(0, self.template_value.columnCount()):
            self.table_column_widths.append(self.template_value.columnWidth(c))

    def save_geometry(self):
        gprefs['template_editor_table_widths'] = self.table_column_widths
        gprefs['template_editor_dialog_geometry'] = bytearray(self.saveGeometry())

    def accept(self):
        txt = unicode_type(self.textbox.toPlainText()).rstrip()
        if self.coloring:
            if self.colored_field.currentIndex() == -1:
                error_dialog(self, _('No column chosen'),
                    _('You must specify a column to be colored'), show=True)
                return
            if not txt:
                error_dialog(self, _('No template provided'),
                    _('The template box cannot be empty'), show=True)
                return

            self.rule = (unicode_type(self.colored_field.itemData(
                                self.colored_field.currentIndex()) or ''), txt)
        elif self.iconing:
            rt = unicode_type(self.icon_kind.itemData(self.icon_kind.currentIndex()) or '')
            self.rule = (rt,
                         unicode_type(self.icon_field.itemData(
                                self.icon_field.currentIndex()) or ''),
                         txt)
        elif self.embleming:
            self.rule = ('icon', 'title', txt)
        else:
            self.rule = ('', txt)
        self.save_geometry()
        QDialog.accept(self)

    def reject(self):
        QDialog.reject(self)
        if self.dialog_is_st_editor:
            parent = self.parent()
            while True:
                if hasattr(parent, 'reject'):
                    parent.reject()
                    break
                parent = parent.parent()
                if parent is None:
                    break


class EmbeddedTemplateDialog(TemplateDialog):

    def __init__(self, parent):
        TemplateDialog.__init__(self, parent, _('A General Program Mode Template'), text_is_placeholder=True,
                                dialog_is_st_editor=True)
        self.setParent(parent)
        self.setWindowFlags(Qt.WindowType.Widget)


if __name__ == '__main__':
    app = QApplication([])
    from calibre.ebooks.metadata.book.base import field_metadata
    d = TemplateDialog(None, '{title}', fm=field_metadata)
    d.exec_()
    del app
