#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import traceback
import urllib
from shutil import copyfile
from xml.etree import ElementTree

import lazylibrarian
import lib.zipfile as zipfile
from lazylibrarian import logger, database
from lazylibrarian.bookwork import setWorkPages
from lazylibrarian.cache import cache_img, get_xml_request
from lazylibrarian.common import opf_file, internet
from lazylibrarian.formatter import plural, is_valid_isbn, is_valid_booktype, getList, unaccented, \
    cleanName, replace_all, split_title
from lazylibrarian.gb import GoogleBooks
from lazylibrarian.gr import GoodReads
from lazylibrarian.importer import update_totals, addAuthorNameToDB
from lib.fuzzywuzzy import fuzz
from lib.mobi import Mobi


def get_book_info(fname):
    # only handles epub, mobi, azw3 and opf for now,
    # for pdf see notes below
    res = {}
    extn = os.path.splitext(fname)[1]
    if not extn:
        return res

    if extn == ".mobi" or extn == ".azw3":
        res['type'] = extn[1:]
        try:
            book = Mobi(fname)
            book.parse()
        except Exception as e:
            logger.debug('Unable to parse mobi in %s, %s' % (fname, str(e)))
            return res
        res['creator'] = book.author()
        res['title'] = book.title()
        res['language'] = book.language()
        res['identifier'] = book.isbn()
        return res

        # noinspection PyUnreachableCode
        """
                # none of the pdfs in my library had language,isbn
                # most didn't have author, or had the wrong author
                # (author set to publisher, or software used)
                # so probably not much point in looking at pdfs
                #
                if (extn == ".pdf"):
                    pdf = PdfFileReader(open(fname, "rb"))
                    txt = pdf.getDocumentInfo()
                    # repackage the data here to get components we need
                    res = {}
                    for s in ['title','language','creator']:
                        res[s] = txt[s]
                    res['identifier'] = txt['isbn']
                    res['type'] = "pdf"
                    return res
                """
    elif extn == ".epub":
        res['type'] = "epub"

        # prepare to read from the .epub file
        try:
            zipdata = zipfile.ZipFile(fname)
        except Exception as e:
            logger.debug('Unable to parse zipfile %s, %s' % (fname, str(e)))
            return res

        # find the contents metafile
        txt = zipdata.read('META-INF/container.xml')
        tree = ElementTree.fromstring(txt)
        n = 0
        cfname = ""
        if not len(tree):
            return res

        while n < len(tree[0]):
            att = tree[0][n].attrib
            if 'full-path' in att:
                cfname = att['full-path']
                break
            n += 1

        # grab the metadata block from the contents metafile
        txt = zipdata.read(cfname)

    elif extn == ".opf":
        res['type'] = "opf"
        txt = open(fname).read()
        # sanitize any unmatched html tags or ElementTree won't parse
        dic = {'<br>': '', '</br>': ''}
        txt = replace_all(txt, dic)
    else:
        logger.error('Unhandled extension in get_book_info: %s' % extn)
        return res

    # repackage epub or opf metadata
    try:
        tree = ElementTree.fromstring(txt)
    except Exception as e:
        logger.error("Error parsing metadata from %s, %s" % (fname, str(e)))
        return res

    if not len(tree):
        return res
    n = 0
    while n < len(tree[0]):
        tag = str(tree[0][n].tag).lower()
        if '}' in tag:
            tag = tag.split('}')[1]
            txt = tree[0][n].text
            attrib = str(tree[0][n].attrib).lower()
            if 'title' in tag:
                res['title'] = txt
            elif 'language' in tag:
                res['language'] = txt
            elif 'creator' in tag and 'creator' not in res:
                # take the first author name if multiple authors
                res['creator'] = txt
            elif 'identifier' in tag and 'isbn' in attrib:
                if is_valid_isbn(txt):
                    res['identifier'] = txt
            elif 'identifier' in tag and 'goodreads' in attrib:
                res['gr_id'] = txt
        n += 1
    return res


def find_book_in_db(myDB, author, book):
    # PAB fuzzy search for book in library, return LL bookid if found or zero
    # if not, return bookid to more easily update status
    # prefer an exact match on author & book
    cmd = 'SELECT BookID FROM books,authors where books.AuthorID = authors.AuthorID '
    cmd += 'and AuthorName="%s" COLLATE NOCASE and BookName="%s" COLLATE NOCASE' % \
            (author.replace('"', '""'), book.replace('"', '""'))
    match = myDB.match(cmd)
    if match:
        logger.debug('Exact match [%s]' % book)
        return match['BookID']
    else:
        # Try a more complex fuzzy match against each book in the db by this author
        # Using hard-coded ratios for now, ratio high (>90), partial_ratio lower (>85)
        # These are results that work well on my library, minimal false matches and no misses
        # on books that should be matched
        # Maybe make ratios configurable in config.ini later
        cmd = 'SELECT BookID,BookName,BookISBN FROM books,authors where books.AuthorID = authors.AuthorID '
        cmd += 'and AuthorName="%s" COLLATE NOCASE' % author.replace('"', '""')
        books = myDB.select(cmd)
        best_ratio = 0
        best_partial = 0
        best_partname = 0
        ratio_name = ""
        partial_name = ""
        partname_name = ""
        ratio_id = 0
        partial_id = 0
        partname_id = 0
        partname = 0

        book_lower = unaccented(book.lower())
        book_partname, book_sub = split_title(author, book_lower)
        if book_partname == book_lower:
            book_partname = ''

        for a_book in books:
            # tidy up everything to raise fuzziness scores
            # still need to lowercase for matching against partial_name later on
            a_book_lower = unaccented(a_book['BookName'].lower())
            #
            ratio = fuzz.ratio(book_lower, a_book_lower)
            partial = fuzz.partial_ratio(book_lower, a_book_lower)
            if book_partname:
                partname = fuzz.partial_ratio(book_partname, a_book_lower)

            # lose a point for each extra word in the fuzzy matches so we get the closest match
            words = len(getList(book_lower))
            words -= len(getList(a_book_lower))
            ratio -= abs(words)
            partial -= abs(words)

            if ratio > best_ratio:
                best_ratio = ratio
                ratio_name = a_book['BookName']
                ratio_id = a_book['BookID']
            if partial > best_partial:
                best_partial = partial
                partial_name = a_book['BookName']
                partial_id = a_book['BookID']
            if partname > best_partname:
                best_partname = partname
                partname_name = a_book['BookName']
                partname_id = a_book['BookID']

            if partial == best_partial:
                # prefer the match closest to the left, ie prefer starting with a match and ignoring the rest
                # this eliminates most false matches against omnibuses when we want a single book
                # find the position of the shortest string in the longest
                if len(getList(book_lower)) >= len(getList(a_book_lower)):
                    match1 = book_lower.find(a_book_lower)
                else:
                    match1 = a_book_lower.find(book_lower)

                if len(getList(book_lower)) >= len(getList(partial_name.lower())):
                    match2 = book_lower.find(partial_name.lower())
                else:
                    match2 = partial_name.lower().find(book_lower)

                if match1 < match2:
                    logger.debug(
                        "Fuzz left change, prefer [%s] over [%s] for [%s]" %
                        (a_book['BookName'], partial_name, book))
                    best_partial = partial
                    partial_name = a_book['BookName']
                    partial_id = a_book['BookID']

        if best_ratio > 90:
            logger.debug(
                "Fuzz match ratio [%d] [%s] [%s]" % (best_ratio, book, ratio_name))
            return ratio_id
        if best_partial > 85:
            logger.debug(
                "Fuzz match partial [%d] [%s] [%s]" % (best_partial, book, partial_name))
            return partial_id
        if best_partname > 95:
            logger.debug(
                "Fuzz match partname [%d] [%s] [%s]" % (best_partname, book, partname_name))
            return partname_id

        if books:
            logger.debug(
                'Fuzz failed [%s - %s] ratio [%d,%s], partial [%d,%s], partname [%d,%s]' %
                (author, book, best_ratio, ratio_name, best_partial, partial_name, best_partname, partname_name))
        else:
            logger.debug('No books found in database for %s' % author)
        return 0



def LibraryScan(startdir=None):
    """ Scan a directory tree adding new books into database
        Return how many books you added """
    try:
        destdir = lazylibrarian.DIRECTORY('Destination')
        if not startdir:
            if not destdir:
                logger.warn('Cannot find destination directory: %s. Not scanning' % destdir)
                return 0
            startdir = destdir

        if not os.path.isdir(startdir):
            logger.warn('Cannot find directory: %s. Not scanning' % startdir)
            return 0

        if not internet():
            logger.warn('Libraryscan: No internet connection')
            return 0

        myDB = database.DBConnection()

        # keep statistics of full library scans
        if startdir == destdir:
            myDB.action('DELETE from stats')
            try:  # remove any extra whitespace in authornames
                authors = myDB.select('SELECT AuthorID,AuthorName FROM authors WHERE AuthorName like "%  %"')
                if authors:
                    logger.info('Removing extra spaces from %s authorname%s' % (len(authors), plural(len(authors))))
                    for author in authors:
                        authorid = author["AuthorID"]
                        authorname = ' '.join(author['AuthorName'].split())
                        # Have we got author name both with-and-without extra spaces? If so, merge them
                        duplicate = myDB.match(
                            'Select AuthorID,AuthorName FROM authors WHERE AuthorName="%s"' % authorname)
                        if duplicate:
                            myDB.action('DELETE from authors where authorname="%s"' % author['AuthorName'])
                            if author['AuthorID'] != duplicate['AuthorID']:
                                myDB.action('UPDATE books set AuthorID="%s" WHERE AuthorID="%s"' %
                                            (duplicate['AuthorID'], author['AuthorID']))
                        else:
                            myDB.action(
                                'UPDATE authors set AuthorName="%s" WHERE AuthorID="%s"' % (authorname, authorid))
            except Exception as e:
                logger.info('Error: ' + str(e))

        logger.info('Scanning ebook directory: %s' % startdir)

        new_book_count = 0
        modified_count = 0
        rescan_count = 0
        rescan_hits = 0
        file_count = 0
        author = ""

        if lazylibrarian.CONFIG['FULL_SCAN']:
            cmd = 'select AuthorName, BookName, BookFile, BookID from books,authors'
            cmd += ' where books.AuthorID = authors.AuthorID and books.Status="Open"'
            if not startdir == destdir:
                cmd += ' and BookFile like "' + startdir + '%"'
            books = myDB.select(cmd)
            status = lazylibrarian.CONFIG['NOTFOUND_STATUS']
            logger.info('Missing books will be marked as %s' % status)
            for book in books:
                bookID = book['BookID']
                bookfile = book['BookFile']

                if not (bookfile and os.path.isfile(bookfile)):
                    myDB.action('update books set Status="%s" where BookID="%s"' % (status, bookID))
                    myDB.action('update books set BookFile="" where BookID="%s"' % bookID)
                    logger.warn('Book %s - %s updated as not found on disk' % (book['AuthorName'], book['BookName']))

        # to save repeat-scans of the same directory if it contains multiple formats of the same book,
        # keep track of which directories we've already looked at
        processed_subdirectories = []
        warned = False  # have we warned about no new authors setting
        matchString = ''
        for char in lazylibrarian.CONFIG['EBOOK_DEST_FILE']:
            matchString = matchString + '\\' + char
        # massage the EBOOK_DEST_FILE config parameter into something we can use
        # with regular expression matching
        booktypes = ''
        count = -1
        booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
        for book_type in booktype_list:
            count += 1
            if count == 0:
                booktypes = book_type
            else:
                booktypes = booktypes + '|' + book_type
        matchString = matchString.replace("\\$\\A\\u\\t\\h\\o\\r", "(?P<author>.*?)").replace(
            "\\$\\T\\i\\t\\l\\e", "(?P<book>.*?)") + '\.[' + booktypes + ']'
        pattern = re.compile(matchString, re.VERBOSE)

        for r, d, f in os.walk(startdir):
            for directory in d[:]:
                # prevent magazine being scanned
                if directory.startswith("_") or directory.startswith("."):
                    d.remove(directory)

            for files in f:
                file_count += 1

                if isinstance(r, str):
                    r = r.decode(lazylibrarian.SYS_ENCODING)

                subdirectory = r.replace(startdir, '')
                # Added new code to skip if we've done this directory before.
                # Made this conditional with a switch in config.ini
                # in case user keeps multiple different books in the same subdirectory
                if lazylibrarian.CONFIG['IMP_SINGLEBOOK'] and (subdirectory in processed_subdirectories):
                    logger.debug("[%s] already scanned" % subdirectory)
                else:
                    # If this is a book, try to get author/title/isbn/language
                    # if epub or mobi, read metadata from the book
                    # If metadata.opf exists, use that allowing it to override
                    # embedded metadata. User may have edited metadata.opf
                    # to merge author aliases together
                    # If all else fails, try pattern match for author/title
                    # and look up isbn/lang from LT or GR later
                    match = 0
                    if is_valid_booktype(files):

                        logger.debug("[%s] Now scanning subdirectory %s" % (startdir, subdirectory))

                        language = "Unknown"
                        isbn = ""
                        book = ""
                        author = ""
                        gr_id = ""
                        gb_id = ""
                        extn = os.path.splitext(files)[1]

                        # if it's an epub or a mobi we can try to read metadata from it
                        if (extn == ".epub") or (extn == ".mobi"):
                            book_filename = os.path.join(r, files).encode(lazylibrarian.SYS_ENCODING)

                            try:
                                res = get_book_info(book_filename)
                            except Exception as e:
                                logger.debug('get_book_info failed for %s, %s' % (book_filename, str(e)))
                                res = {}
                            # title and creator are the minimum we need
                            if 'title' in res and 'creator' in res:
                                book = res['title']
                                author = res['creator']
                                if book and len(book) > 2 and author and len(author) > 2:
                                    match = 1
                                if 'language' in res:
                                    language = res['language']
                                if 'identifier' in res:
                                    isbn = res['identifier']
                                if 'type' in res:
                                    extn = res['type']
                                logger.debug("book meta [%s] [%s] [%s] [%s] [%s]" %
                                             (isbn, language, author, book, extn))
                            if not match:
                                logger.debug("Book meta incomplete in %s" % book_filename)

                        # calibre uses "metadata.opf", LL uses "bookname - authorname.opf"
                        # just look for any .opf file in the current directory since we don't know
                        # LL preferred authorname/bookname at this point.
                        # Allow metadata in file to override book contents as may be users pref

                        metafile = opf_file(r)
                        try:
                            res = get_book_info(metafile)
                        except Exception as e:
                            logger.debug('get_book_info failed for %s, %s' % (metafile, str(e)))
                            res = {}
                        # title and creator are the minimum we need
                        if 'title' in res and 'creator' in res:
                            book = res['title']
                            author = res['creator']
                            if book and len(book) > 2 and author and len(author) > 2:
                                match = 1
                            if 'language' in res:
                                language = res['language']
                            if 'identifier' in res:
                                isbn = res['identifier']
                            if 'gr_id' in res:
                                gr_id = res['gr_id']
                            logger.debug("file meta [%s] [%s] [%s] [%s] [%s]" % (isbn, language, author, book, gr_id))
                        if not match:
                            logger.debug("File meta incomplete in %s" % metafile)

                        if not match:  # no author/book from metadata file, and not embedded either
                            match = pattern.match(files)
                            if match:
                                author = match.group("author")
                                book = match.group("book")
                                if len(book) <= 2 or len(author) <= 2:
                                    match = 0
                            if not match:
                                logger.debug("Pattern match failed [%s]" % files)

                        if match:
                            # flag that we found a book in this subdirectory
                            processed_subdirectories.append(subdirectory)

                            # If we have a valid looking isbn, and language != "Unknown", add it to cache
                            if language != "Unknown" and is_valid_isbn(isbn):
                                logger.debug("Found Language [%s] ISBN [%s]" % (language, isbn))
                                # we need to add it to language cache if not already
                                # there, is_valid_isbn has checked length is 10 or 13
                                if len(isbn) == 10:
                                    isbnhead = isbn[0:3]
                                else:
                                    isbnhead = isbn[3:6]
                                match = myDB.match('SELECT lang FROM languages where isbn = "%s"' % isbnhead)
                                if not match:
                                    myDB.action('insert into languages values ("%s", "%s")' % (isbnhead, language))
                                    logger.debug("Cached Lang [%s] ISBN [%s]" % (language, isbnhead))
                                else:
                                    logger.debug("Already cached Lang [%s] ISBN [%s]" % (language, isbnhead))

                            author, authorid, new = addAuthorNameToDB(author)  # get the author name as we know it...

                            if author:
                                # author exists, check if this book by this author is in our database
                                # metadata might have quotes in book name
                                # some books might be stored under a different author name
                                # eg books by multiple authors, books where author is "writing as"
                                # or books we moved to "merge" authors
                                book = book.replace("'", "")

                                # First try and find it under author and bookname
                                # as we may have it under a different bookid or isbn to goodreads/googlebooks
                                # which might have several bookid/isbn for the same book
                                bookid = find_book_in_db(myDB, author, book)

                                if not bookid:
                                    # Title or author name might not match or multiple authors
                                    # See if the gr_id, gb_id is already in our database
                                    if gr_id:
                                        bookid = gr_id
                                    elif gb_id:
                                        bookid = gb_id
                                    else:
                                        bookid = ""

                                    if bookid:
                                        match = myDB.match('SELECT BookID FROM books where BookID = "%s"' % bookid)
                                        if not match:
                                            msg = 'Unable to find book %s by %s in database, trying to add it using '
                                            if bookid == gr_id:
                                                msg += "GoodReads ID " + gr_id
                                            if bookid == gb_id:
                                                msg += "GoogleBooks ID " + gb_id
                                            logger.debug(msg % (book, author))
                                            if lazylibrarian.CONFIG['BOOK_API'] == "GoodReads" and gr_id:
                                                GR_ID = GoodReads(gr_id)
                                                GR_ID.find_book(gr_id, None)
                                            elif lazylibrarian.CONFIG['BOOK_API'] == "GoogleBooks" and gb_id:
                                                GB_ID = GoogleBooks(gb_id)
                                                GB_ID.find_book(gb_id, None)
                                            # see if it's there now...
                                            match = myDB.match('SELECT BookID from books where BookID="%s"' % bookid)
                                            if not match:
                                                logger.debug("Unable to add bookid %s to database" % bookid)
                                                bookid = ""

                                if not bookid and isbn:
                                    # See if the isbn is in our database
                                    match = myDB.match('SELECT BookID FROM books where BookIsbn = "%s"' % isbn)
                                    if match:
                                        bookid = match['BookID']

                                if not bookid:
                                    # get author name from parent directory of this book directory
                                    newauthor = os.path.basename(os.path.dirname(r))
                                    # calibre replaces trailing periods with _ eg Smith Jr. -> Smith Jr_
                                    if newauthor.endswith('_'):
                                        newauthor = newauthor[:-1] + '.'
                                    if author.lower() != newauthor.lower():
                                        logger.debug("Trying authorname [%s]" % newauthor)
                                        bookid = find_book_in_db(myDB, newauthor, book)
                                        if bookid:
                                            logger.warn("%s not found under [%s], found under [%s]" %
                                                        (book, author, newauthor))

                                # at this point if we still have no bookid, it looks like we
                                # have author and book title but no database entry for it
                                if not bookid:
                                    if lazylibrarian.CONFIG['BOOK_API'] == "GoodReads":
                                        # Either goodreads doesn't have the book or it didn't match language prefs
                                        # Since we have the book anyway, try and reload it ignoring language prefs
                                        rescan_count += 1
                                        base_url = 'http://www.goodreads.com/search.xml?q='
                                        params = {"key": lazylibrarian.CONFIG['GR_API']}
                                        if author[1] in '. ':
                                            surname = author
                                            forename = ''
                                            while surname[1] in '. ':
                                                forename = forename + surname[0] + '.'
                                                surname = surname[2:].strip()
                                            if author != forename + ' ' + surname:
                                                logger.debug('Stripped authorname [%s] to [%s %s]' %
                                                            (author, forename, surname))
                                                author = forename + ' ' + surname

                                        author = ' '.join(author.split())  # ensure no extra whitespace

                                        searchname = author + ' ' + book
                                        searchname = cleanName(unaccented(searchname))
                                        searchterm = urllib.quote_plus(searchname.encode(lazylibrarian.SYS_ENCODING))
                                        set_url = base_url + searchterm + '&' + urllib.urlencode(params)
                                        try:
                                            rootxml, in_cache = get_xml_request(set_url)
                                            if not len(rootxml):
                                                logger.debug("Error requesting results from GoodReads")
                                            else:
                                                resultxml = rootxml.getiterator('work')
                                                for item in resultxml:
                                                    booktitle = item.find('./best_book/title').text
                                                    book_fuzz = fuzz.token_set_ratio(booktitle, book)
                                                    if book_fuzz >= 98:
                                                        logger.debug("Rescan found %s : %s" % (booktitle, language))
                                                        rescan_hits += 1
                                                        bookid = item.find('./best_book/id').text
                                                        GR_ID = GoodReads(bookid)
                                                        GR_ID.find_book(bookid, None)
                                                        if language and language != "Unknown":
                                                            # set language from book metadata
                                                            logger.debug("Setting language from metadata %s : %s" % (booktitle, language))
                                                            myDB.action('UPDATE books SET BookLang="%s" WHERE BookID="%s"' %
                                                                        (language, bookid))
                                                        break
                                                if not bookid:
                                                    logger.warn("GoodReads doesn't know about %s" % book)
                                        except Exception as e:
                                            logger.error("Error finding rescan results: %s" % str(e))

                                    elif lazylibrarian.CONFIG['BOOK_API'] == "GoogleBooks":
                                        # if we get here using googlebooks it's because googlebooks
                                        # doesn't have the book. No point in looking for it again.
                                        logger.warn("GoogleBooks doesn't know about %s" % book)

                                # see if it's there now...
                                if bookid:
                                    cmd = 'SELECT books.Status, BookFile, AuthorName, BookName from books,authors '
                                    cmd += 'where books.AuthorID = authors.AuthorID and BookID="%s"' % bookid
                                    check_status = myDB.match(cmd)

                                    if not check_status:
                                        logger.debug('Unable to find bookid %s in database' % bookid)
                                    else:
                                        if check_status['Status'] != 'Open':
                                            # we found a new book
                                            new_book_count += 1
                                            myDB.action(
                                                'UPDATE books set Status="Open" where BookID="%s"' % bookid)

                                        # store book location so we can check if it gets removed
                                        book_filename = os.path.join(r, files)
                                        if not check_status['BookFile']:  # no previous location
                                            myDB.action('UPDATE books set BookFile="%s" where BookID="%s"' %
                                                        (book_filename, bookid))
                                        # location may have changed since last scan
                                        elif book_filename != check_status['BookFile']:
                                            modified_count += 1
                                            logger.warn("Updating book location for %s %s from %s to %s" %
                                                        (author, book, check_status['BookFile'], book_filename))
                                            logger.debug("%s %s matched %s BookID %s, [%s][%s]" %
                                                        (author, book, check_status['Status'], bookid,
                                                        check_status['AuthorName'], check_status['BookName']))
                                            myDB.action('UPDATE books set BookFile="%s" where BookID="%s"' %
                                                        (book_filename, bookid))

                                        # update cover file to cover.jpg in book folder (if exists)
                                        bookdir = os.path.dirname(book_filename)
                                        coverimg = os.path.join(bookdir, 'cover.jpg')
                                        if os.path.isfile(coverimg):
                                            cachedir = lazylibrarian.CACHEDIR
                                            cacheimg = os.path.join(cachedir, 'book', bookid + '.jpg')
                                            copyfile(coverimg, cacheimg)
                                else:
                                    logger.warn(
                                        "Failed to match book [%s] by [%s] in database" % (book, author))
                            else:
                                if not warned and not lazylibrarian.CONFIG['ADD_AUTHOR']:
                                    logger.warn("Add authors to database is disabled")
                                    warned = True

        logger.info("%s/%s new/modified book%s found and added to the database" %
                    (new_book_count, modified_count, plural(new_book_count + modified_count)))
        logger.info("%s file%s processed" % (file_count, plural(file_count)))

        if startdir == destdir:
            # On full library scans, check for missing workpages
            setWorkPages()
            # and books with unknown language
            nolang = myDB.match(
                "select count('BookID') as counter from Books where status='Open' and BookLang='Unknown'")
            nolang = nolang['counter']
            if nolang:
                logger.warn("Found %s book%s in your library with unknown language" % (nolang, plural(nolang)))
                # show stats if new books were added
            stats = myDB.match(
                "SELECT sum(GR_book_hits), sum(GR_lang_hits), sum(LT_lang_hits), sum(GB_lang_change), \
                    sum(cache_hits), sum(bad_lang), sum(bad_char), sum(uncached), sum(duplicates) FROM stats")

            st= {'GR_book_hits': stats['sum(GR_book_hits)'], 'GB_book_hits': stats['sum(GR_book_hits)'],
                 'GR_lang_hits': stats['sum(GR_lang_hits)'], 'LT_lang_hits': stats['sum(LT_lang_hits)'],
                 'GB_lang_change': stats['sum(GB_lang_change)'], 'cache_hits': stats['sum(cache_hits)'],
                 'bad_lang': stats['sum(bad_lang)'], 'bad_char': stats['sum(bad_char)'],
                 'uncached': stats['sum(uncached)'], 'duplicates': stats['sum(duplicates)']}

            for item in st.keys():
                if st[item] is None:
                    st[item] = 0

            if lazylibrarian.CONFIG['BOOK_API'] == "GoogleBooks":
                logger.debug("GoogleBooks was hit %s time%s for books" %
                             (st['GR_book_hits'], plural(st['GR_book_hits'])))
                logger.debug("GoogleBooks language was changed %s time%s" %
                             (st['GB_lang_change'], plural(st['GB_lang_change'])))
            if lazylibrarian.CONFIG['BOOK_API'] == "GoodReads":
                logger.debug("GoodReads was hit %s time%s for books" %
                             (st['GR_book_hits'], plural(st['GR_book_hits'])))
                logger.debug("GoodReads was hit %s time%s for languages" %
                             (st['GR_lang_hits'], plural(st['GR_lang_hits'])))
            logger.debug("LibraryThing was hit %s time%s for languages" %
                         (st['LT_lang_hits'], plural(st['LT_lang_hits'])))
            logger.debug("Language cache was hit %s time%s" %
                         (st['cache_hits'], plural(st['cache_hits'])))
            logger.debug("Unwanted language removed %s book%s" %
                         (st['bad_lang'], plural(st['bad_lang'])))
            logger.debug("Unwanted characters removed %s book%s" %
                         (st['bad_char'], plural(st['bad_char'])))
            logger.debug("Unable to cache language for %s book%s with missing ISBN" %
                         (st['uncached'], plural(st['uncached'])))
            logger.debug("Found %s duplicate book%s" %
                         (st['duplicates'], plural(st['duplicates'])))
            logger.debug("Rescan %s hit%s, %s miss" %
                         (rescan_hits, plural(rescan_hits), rescan_count - rescan_hits))
            logger.debug("Cache %s hit%s, %s miss" %
                         (lazylibrarian.CACHE_HIT, plural(lazylibrarian.CACHE_HIT), lazylibrarian.CACHE_MISS))
            cachesize = myDB.match("select count('ISBN') as counter from languages")
            logger.debug("ISBN Language cache holds %s entries" % cachesize['counter'])

            # Cache any covers and images
            images = myDB.select('select bookid, bookimg, bookname from books where bookimg like "http%"')
            if len(images):
                logger.info("Caching cover%s for %i book%s" % (plural(len(images)), len(images), plural(len(images))))
                for item in images:
                    bookid = item['bookid']
                    bookimg = item['bookimg']
                    # bookname = item['bookname']
                    newimg, success = cache_img("book", bookid, bookimg)
                    if success:
                        myDB.action('update books set BookImg="%s" where BookID="%s"' % (newimg, bookid))

            images = myDB.select('select AuthorID, AuthorImg, AuthorName from authors where AuthorImg like "http%"')
            if len(images):
                logger.info("Caching image%s for %i author%s" % (plural(len(images)), len(images), plural(len(images))))
                for item in images:
                    authorid = item['authorid']
                    authorimg = item['authorimg']
                    # authorname = item['authorname']
                    newimg, success = cache_img("author", authorid, authorimg)
                    if success:
                        myDB.action('update authors set AuthorImg="%s" where AuthorID="%s"' % (newimg, authorid))

            # On full scan, update bookcounts for all authors, not just new ones - refresh may have located
            # new books for existing authors especially if switched provider gb/gr or changed wanted languages
            authors = myDB.select('select AuthorID from authors')
        else:
            # On single author/book import, just update bookcount for that author
            authors = myDB.select('select AuthorID from authors where AuthorName = "%s"' % author.replace('"', '""'))

        logger.debug('Updating bookcounts for %i author%s' % (len(authors), plural(len(authors))))
        for author in authors:
            update_totals(author['AuthorID'])

        logger.info('Library scan complete')
        return new_book_count

    except Exception:
        logger.error('Unhandled exception in libraryScan: %s' % traceback.format_exc())
