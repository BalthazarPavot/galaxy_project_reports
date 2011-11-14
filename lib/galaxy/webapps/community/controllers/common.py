import os, string, socket, logging, simplejson, binascii
from time import strftime
from datetime import *
from galaxy.tools import *
from galaxy.util.json import from_json_string, to_json_string
from galaxy.util.hash_util import *
from galaxy.web.base.controller import *
from galaxy.webapps.community import model
from galaxy.model.orm import *
from galaxy.model.item_attrs import UsesItemRatings
from mercurial import hg, ui, commands

log = logging.getLogger( __name__ )

email_alert_template = """
GALAXY TOOL SHED REPOSITORY UPDATE ALERT
-----------------------------------------------------------------------------
You received this alert because you registered to receive email whenever
changes were made to the repository named "${repository_name}".
-----------------------------------------------------------------------------

Date of change: ${display_date}
Changed by:     ${username}

Revision: ${revision}
Change description:
${description}

-----------------------------------------------------------------------------
This change alert was sent from the Galaxy tool shed hosted on the server
"${host}"
"""

contact_owner_template = """
GALAXY TOOL SHED REPOSITORY MESSAGE
------------------------

The user '${username}' sent you the following message regarding your tool shed
repository named '${repository_name}'.  You can respond by sending a reply to
the user's email address: ${email}.
-----------------------------------------------------------------------------
${message}
-----------------------------------------------------------------------------
This message was sent from the Galaxy Tool Shed instance hosted on the server
'${host}'
"""

# States for passing messages
SUCCESS, INFO, WARNING, ERROR = "done", "info", "warning", "error"

malicious_error = "  This changeset cannot be downloaded because it potentially produces malicious behavior or contains inappropriate content."
malicious_error_can_push = "  Correct this changeset as soon as possible, it potentially produces malicious behavior or contains inappropriate content."

class ItemRatings( UsesItemRatings ):
    """Overrides rate_item method since we also allow for comments"""
    def rate_item( self, trans, user, item, rating, comment='' ):
        """ Rate an item. Return type is <item_class>RatingAssociation. """
        item_rating = self.get_user_item_rating( trans.sa_session, user, item, webapp_model=trans.model )
        if not item_rating:
            # User has not yet rated item; create rating.
            item_rating_assoc_class = self._get_item_rating_assoc_class( item, webapp_model=trans.model )
            item_rating = item_rating_assoc_class()
            item_rating.user = trans.user
            item_rating.set_item( item )
            item_rating.rating = rating
            item_rating.comment = comment
            trans.sa_session.add( item_rating )
            trans.sa_session.flush()
        elif item_rating.rating != rating or item_rating.comment != comment:
            # User has previously rated item; update rating.
            item_rating.rating = rating
            item_rating.comment = comment
            trans.sa_session.add( item_rating )
            trans.sa_session.flush()
        return item_rating

## ---- Utility methods -------------------------------------------------------

def get_categories( trans ):
    """Get all categories from the database"""
    return trans.sa_session.query( trans.model.Category ) \
                           .filter( trans.model.Category.table.c.deleted==False ) \
                           .order_by( trans.model.Category.table.c.name ).all()
def get_category( trans, id ):
    """Get a category from the database"""
    return trans.sa_session.query( trans.model.Category ).get( trans.security.decode_id( id ) )
def get_repository( trans, id ):
    """Get a repository from the database via id"""
    return trans.sa_session.query( trans.model.Repository ).get( trans.security.decode_id( id ) )
def get_repository_by_name_and_owner( trans, name, owner ):
    """Get a repository from the database via name and owner"""
    user = get_user_by_username( trans, owner )
    return trans.sa_session.query( trans.model.Repository ) \
                             .filter( and_( trans.model.Repository.table.c.name == name,
                                            trans.model.Repository.table.c.user_id == user.id ) ) \
                             .first()
def get_repository_metadata_by_changeset_revision( trans, id, changeset_revision ):
    """Get metadata for a specified repository change set from the database"""
    return trans.sa_session.query( trans.model.RepositoryMetadata ) \
                           .filter( and_( trans.model.RepositoryMetadata.table.c.repository_id == trans.security.decode_id( id ),
                                          trans.model.RepositoryMetadata.table.c.changeset_revision == changeset_revision ) ) \
                           .first()
def get_repository_metadata_by_id( trans, id ):
    """Get repository metadata from the database"""
    return trans.sa_session.query( trans.model.RepositoryMetadata ).get( trans.security.decode_id( id ) )
def get_repository_metadata_by_repository_id( trans, id ):
    """Get all metadata records for a specified repository."""
    return trans.sa_session.query( trans.model.RepositoryMetadata ) \
                           .filter( trans.model.RepositoryMetadata.table.c.repository_id == trans.security.decode_id( id ) )
def get_revision_label( trans, repository, changeset_revision ):
    """
    Return a string consisting of the human read-able 
    changeset rev and the changeset revision string.
    """
    repo = hg.repository( get_configured_ui(), repository.repo_path )
    ctx = get_changectx_for_changeset( trans, repo, changeset_revision )
    if ctx:
        return "%s:%s" % ( str( ctx.rev() ), changeset_revision )
    else:
        return "-1:%s" % changeset_revision
def get_latest_repository_metadata( trans, id ):
    """Get last metadata defined for a specified repository from the database"""
    return trans.sa_session.query( trans.model.RepositoryMetadata ) \
                           .filter( trans.model.RepositoryMetadata.table.c.repository_id == trans.security.decode_id( id ) ) \
                           .order_by( trans.model.RepositoryMetadata.table.c.id.desc() ) \
                           .first()
def generate_clone_url( trans, repository_id ):
    repository = get_repository( trans, repository_id )
    protocol, base = trans.request.base.split( '://' )
    if trans.user:
        username = '%s@' % trans.user.username
    else:
        username = ''
    return '%s://%s%s/repos/%s/%s' % ( protocol, username, base, repository.user.username, repository.name )
def generate_tool_guid( trans, repository, tool ):
    """
    Generate a guid for the received tool.  The form of the guid is    
    <tool shed host>/repos/<tool shed username>/<tool shed repo name>/<tool id>/<tool version>
    """
    return '%s/repos/%s/%s/%s/%s' % ( trans.request.host,
                                      repository.user.username,
                                      repository.name,
                                      tool.id,
                                      tool.version )
def check_tool_input_params( trans, name, tool, sample_files, invalid_files ):
    """
    Check all of the tool's input parameters, looking for any that are dynamically generated
    using external data files to make sure the files exist.
    """
    can_set_metadata = True
    correction_msg = ''
    for input_param in tool.input_params:
        if isinstance( input_param, galaxy.tools.parameters.basic.SelectToolParameter ) and input_param.is_dynamic:
            # If the tool refers to .loc files or requires an entry in the
            # tool_data_table_conf.xml, make sure all requirements exist.
            options = input_param.dynamic_options or input_param.options
            if options:
                if options.tool_data_table or options.missing_tool_data_table_name:
                    # Make sure the repository contains a tool_data_table_conf.xml.sample file.
                    sample_found = False
                    for sample_file in sample_files:
                        head, tail = os.path.split( sample_file )
                        if tail == 'tool_data_table_conf.xml.sample':
                            sample_found = True
                            error, correction_msg = handle_sample_tool_data_table_conf_file( trans, sample_file )
                            if error:
                                can_set_metadata = False
                                invalid_files.append( ( tail, correction_msg ) ) 
                            else:
                                options.missing_tool_data_table_name = None
                            break
                    if not sample_found:
                        can_set_metadata = False
                        correction_msg = "This file requires an entry in the tool_data_table_conf.xml file.  "
                        correction_msg += "Upload a file named tool_data_table_conf.xml.sample to the repository "
                        correction_msg += "that includes the required entry to resolve this issue.<br/>"
                        invalid_files.append( ( name, correction_msg ) )
                if options.index_file or options.missing_index_file:
                    # Make sure the repository contains the required xxx.loc.sample file.
                    index_file = options.index_file or options.missing_index_file
                    index_head, index_tail = os.path.split( index_file )
                    sample_found = False
                    for sample_file in sample_files:
                        sample_head, sample_tail = os.path.split( sample_file )
                        if sample_tail == '%s.sample' % index_tail:
                            copy_sample_loc_file( trans, sample_file )
                            options.index_file = index_tail
                            options.missing_index_file = None
                            if options.tool_data_table:
                                options.tool_data_table.missing_index_file = None
                            sample_found = True
                            break
                    if not sample_found:
                        can_set_metadata = False
                        correction_msg = "This file refers to a file named <b>%s</b>.  " % str( index_file )
                        correction_msg += "Upload a file named <b>%s.sample</b> to the repository to correct this error." % str( index_tail )
                        invalid_files.append( ( name, correction_msg ) )
    return can_set_metadata, invalid_files
def generate_tool_metadata( trans, id, changeset_revision, tool_config, tool, metadata_dict ):
    """
    Update the received metadata_dict with changes that have been
    applied to the received tool.
    """
    repository = get_repository( trans, id )
    # Handle tool.requirements.
    tool_requirements = []
    for tr in tool.requirements:
        name=tr.name
        type=tr.type
        if type == 'fabfile':
            version = None
            fabfile = tr.fabfile
            method = tr.method
        else:
            version = tr.version
            fabfile = None
            method = None
        requirement_dict = dict( name=name,
                                 type=type,
                                 version=version,
                                 fabfile=fabfile,
                                 method=method )
        tool_requirements.append( requirement_dict )
    # Handle tool.tests.
    tool_tests = []
    if tool.tests:
        for ttb in tool.tests:
            test_dict = dict( name=ttb.name,
                              required_files=ttb.required_files,
                              inputs=ttb.inputs,
                              outputs=ttb.outputs )
            tool_tests.append( test_dict )
    tool_dict = dict( id=tool.id,
                      guid = generate_tool_guid( trans, repository, tool ),
                      name=tool.name,
                      version=tool.version,
                      description=tool.description,
                      version_string_cmd = tool.version_string_cmd,
                      tool_config=tool_config,
                      requirements=tool_requirements,
                      tests=tool_tests )
    if 'tools' in metadata_dict:
        metadata_dict[ 'tools' ].append( tool_dict )
    else:
        metadata_dict[ 'tools' ] = [ tool_dict ]
    return metadata_dict
def new_tool_metadata_required( trans, id, metadata_dict ):
    """
    Compare the last saved metadata for each tool in the repository with the new metadata
    in metadata_dict to determine if a new repository_metadata table record is required, or
    if the last saved metadata record can updated instead.
    """
    if 'tools' in metadata_dict:
        repository_metadata = get_latest_repository_metadata( trans, id )
        if repository_metadata:
            metadata = repository_metadata.metadata
            if metadata and 'tools' in metadata:
                saved_tool_ids = []
                # The metadata for one or more tools was successfully generated in the past
                # for this repository, so we first compare the version string for each tool id
                # in metadata_dict with what was previously saved to see if we need to create
                # a new table record or if we can simply update the existing record.
                for new_tool_metadata_dict in metadata_dict[ 'tools' ]:
                    for saved_tool_metadata_dict in metadata[ 'tools' ]:
                        if saved_tool_metadata_dict[ 'id' ] not in saved_tool_ids:
                            saved_tool_ids.append( saved_tool_metadata_dict[ 'id' ] )
                        if new_tool_metadata_dict[ 'id' ] == saved_tool_metadata_dict[ 'id' ]:
                            if new_tool_metadata_dict[ 'version' ] != saved_tool_metadata_dict[ 'version' ]:
                                return True
                # So far, a new metadata record is not required, but we still have to check to see if
                # any new tool ids exist in metadata_dict that are not in the saved metadata.  We do
                # this because if a new tarball was uploaded to a repository that included tools, it
                # may have removed existing tool files if they were not included in the uploaded tarball.
                for new_tool_metadata_dict in metadata_dict[ 'tools' ]:
                    if new_tool_metadata_dict[ 'id' ] not in saved_tool_ids:
                        return True
            else:
                # We have repository metadata that does not include metadata for any tools in the
                # repository, so we can update the existing repository metadata.
                return False
        else:
            # There is no saved repository metadata, so we need to create a new repository_metadata
            # table record.
            return True
    # The received metadata_dict includes no metadata for tools, so a new repository_metadata table
    # record is not needed.
    return False
def generate_workflow_metadata( trans, id, changeset_revision, exported_workflow_dict, metadata_dict ):
    """
    Update the received metadata_dict with changes that have been applied
    to the received exported_workflow_dict.  Store everything in the database.
    """
    if 'workflows' in metadata_dict:
        metadata_dict[ 'workflows' ].append( exported_workflow_dict )
    else:
        metadata_dict[ 'workflows' ] = [ exported_workflow_dict ]
    return metadata_dict
def new_workflow_metadata_required( trans, id, metadata_dict ):
    """
    Currently everything about an exported workflow except the name is hard-coded, so there's
    no real way to differentiate versions of exported workflows.  If this changes at some future
    time, this method should be enhanced accordingly.
    """
    if 'workflows' in metadata_dict:
        repository_metadata = get_latest_repository_metadata( trans, id )
        if repository_metadata:
            if repository_metadata.metadata:
                # The repository has metadata, so update the workflows value - no new record is needed.
                return False
        else:
            # There is no saved repository metadata, so we need to create a new repository_metadata table record.
            return True
    # The received metadata_dict includes no metadata for workflows, so a new repository_metadata table
    # record is not needed.
    return False
def generate_datatypes_metadata( trans, id, changeset_revision, datatypes_config, metadata_dict ):
    """
    Update the received metadata_dict with changes that have been applied
    to the received datatypes_config.
    """
    # Parse datatypes_config.
    tree = ElementTree.parse( datatypes_config )
    root = tree.getroot()
    ElementInclude.include( root )
    repository_datatype_code_files = []
    datatype_files = root.find( 'datatype_files' )
    if datatype_files:
        for elem in datatype_files.findall( 'datatype_file' ):
            name = elem.get( 'name', None )
            repository_datatype_code_files.append( name )
        metadata_dict[ 'datatype_files' ] = repository_datatype_code_files
    datatypes = []
    registration = root.find( 'registration' )
    if registration:
        for elem in registration.findall( 'datatype' ):
            extension = elem.get( 'extension', None ) 
            dtype = elem.get( 'type', None )
            mimetype = elem.get( 'mimetype', None )
            datatypes.append( dict( extension=extension,
                                    dtype=dtype,
                                    mimetype=mimetype ) )
        metadata_dict[ 'datatypes' ] = datatypes
    return metadata_dict
def set_repository_metadata( trans, id, changeset_revision, **kwd ):
    """Set repository metadata"""
    message = ''
    status = 'done'
    repository = get_repository( trans, id )
    repo_dir = repository.repo_path
    repo = hg.repository( get_configured_ui(), repo_dir )
    invalid_files = []
    sample_files = []
    datatypes_config = None
    ctx = get_changectx_for_changeset( trans, repo, changeset_revision )
    if ctx is not None:
        metadata_dict = {}
        if changeset_revision == repository.tip:
            # Find datatypes_conf.xml if it exists.
            for root, dirs, files in os.walk( repo_dir ):
                if root.find( '.hg' ) < 0:
                    for name in files:
                        if name == 'datatypes_conf.xml':
                            datatypes_config = os.path.abspath( os.path.join( root, name ) )
                            break
            if datatypes_config:
                metadata_dict = generate_datatypes_metadata( trans, id, changeset_revision, datatypes_config, metadata_dict )
            # Find all special .sample files.
            for root, dirs, files in os.walk( repo_dir ):
                if root.find( '.hg' ) < 0:
                    for name in files:
                        if name.endswith( '.sample' ):
                            sample_files.append( os.path.abspath( os.path.join( root, name ) ) )
            # Find all tool configs and exported workflows.
            for root, dirs, files in os.walk( repo_dir ):
                if root.find( '.hg' ) < 0 and root.find( 'hgrc' ) < 0:
                    if '.hg' in dirs:
                        dirs.remove( '.hg' )
                    for name in files:
                        # Find all tool configs.
                        if name != 'datatypes_conf.xml' and name.endswith( '.xml' ):
                            try:
                                full_path = os.path.abspath( os.path.join( root, name ) )
                                tool = load_tool( trans, full_path )
                                if tool is not None:
                                    can_set_metadata, invalid_files = check_tool_input_params( trans, name, tool, sample_files, invalid_files )
                                    if can_set_metadata:
                                        # Update the list of metadata dictionaries for tools in metadata_dict.
                                        tool_config = os.path.join( root, name )
                                        metadata_dict = generate_tool_metadata( trans, id, changeset_revision, tool_config, tool, metadata_dict )
                            except Exception, e:
                                invalid_files.append( ( name, str( e ) ) )
                        # Find all exported workflows
                        elif name.endswith( '.ga' ):
                            try:
                                full_path = os.path.abspath( os.path.join( root, name ) )
                                # Convert workflow data from json
                                fp = open( full_path, 'rb' )
                                workflow_text = fp.read()
                                fp.close()
                                exported_workflow_dict = from_json_string( workflow_text )
                                if exported_workflow_dict[ 'a_galaxy_workflow' ] == 'true':
                                    # Update the list of metadata dictionaries for workflows in metadata_dict.
                                    metadata_dict = generate_workflow_metadata( trans, id, changeset_revision, exported_workflow_dict, metadata_dict )
                            except Exception, e:
                                invalid_files.append( ( name, str( e ) ) )
        else:
            # Find all special .sample files first.
            for filename in ctx:
                if filename.endswith( '.sample' ):
                    sample_files.append( os.path.abspath( filename ) )
            # Get all tool config file names from the hgweb url, something like:
            # /repos/test/convert_chars1/file/e58dcf0026c7/convert_characters.xml
            for filename in ctx:
                # Find all tool configs - we do not have to update metadata for workflows or datatypes in anything
                # but repository tips (handled above) since at the time this code was written, no workflows or
                # dataytpes_conf.xml files exist in tool shed repositories, so they can only be added in future tips.
                if filename.endswith( '.xml' ):
                    fctx = ctx[ filename ]
                    # Write the contents of the old tool config to a temporary file.
                    fh = tempfile.NamedTemporaryFile( 'w' )
                    tmp_filename = fh.name
                    fh.close()
                    fh = open( tmp_filename, 'w' )
                    fh.write( fctx.data() )
                    fh.close()
                    try:
                        tool = load_tool( trans, tmp_filename )
                        if tool is not None:
                            can_set_metadata, invalid_files = check_tool_input_params( trans, filename, tool, sample_files, invalid_files )
                            if can_set_metadata:
                                # Update the list of metadata dictionaries for tools in metadata_dict.  Note that filename
                                # here is the relative path to the config file within the change set context, something
                                # like filtering.xml, but when the change set was the repository tip, the value was
                                # something like database/community_files/000/repo_1/filtering.xml.  This shouldn't break
                                # anything, but may result in a bit of confusion when maintaining the code / data over time.
                                metadata_dict = generate_tool_metadata( trans, id, changeset_revision, filename, tool, metadata_dict )
                    except Exception, e:
                        invalid_files.append( ( name, str( e ) ) )
                    try:
                        os.unlink( tmp_filename )
                    except:
                        pass
        if metadata_dict:
            if changeset_revision == repository.tip:
                if new_tool_metadata_required( trans, id, metadata_dict ) or new_workflow_metadata_required( trans, id, metadata_dict ):
                    # Create a new repository_metadata table row.
                    repository_metadata = trans.model.RepositoryMetadata( repository.id, changeset_revision, metadata_dict )
                    trans.sa_session.add( repository_metadata )
                    trans.sa_session.flush()
                else:
                    # Update the last saved repository_metadata table row.
                    repository_metadata = get_latest_repository_metadata( trans, id )
                    repository_metadata.changeset_revision = changeset_revision
                    repository_metadata.metadata = metadata_dict
                    trans.sa_session.add( repository_metadata )
                    trans.sa_session.flush()
            else:
                # We're re-generating metadata for an old repository revision.
                repository_metadata = get_repository_metadata_by_changeset_revision( trans, id, changeset_revision )
                repository_metadata.metadata = metadata_dict
                trans.sa_session.add( repository_metadata )
                trans.sa_session.flush()
        else:
            message = "Revision '%s' includes no tools or exported workflows for which metadata can be defined " % str( changeset_revision )
            message += "so this revision cannot be automatically installed into a local Galaxy instance."
            status = "error"
    else:
        # change_set is None
        message = "This repository does not include revision '%s'." % str( changeset_revision )
        status = 'error'
    if invalid_files:
        if metadata_dict:
            message = "Metadata was defined for some items in revision '%s'.  " % str( changeset_revision )
            message += "Correct the following problems if necessary and reset metadata.<br/>"
        else:
            message = "Metadata cannot be defined for revision '%s' so this revision cannot be automatically " % str( changeset_revision )
            message += "installed into a local Galaxy instance.  Correct the following problems and reset metadata.<br/>"
        for itc_tup in invalid_files:
            tool_file, exception_msg = itc_tup
            if exception_msg.find( 'No such file or directory' ) >= 0:
                exception_items = exception_msg.split()
                missing_file_items = exception_items[7].split( '/' )
                missing_file = missing_file_items[-1].rstrip( '\'' )
                if missing_file.endswith( '.loc' ):
                    sample_ext = '%s.sample' % missing_file
                else:
                    sample_ext = missing_file
                correction_msg = "This file refers to a missing file <b>%s</b>.  " % str( missing_file )
                correction_msg += "Upload a file named <b>%s</b> to the repository to correct this error." % sample_ext
            else:
               correction_msg = exception_msg
            message += "<b>%s</b> - %s<br/>" % ( tool_file, correction_msg )
        status = 'error'
    return message, status
def get_repository_by_name( trans, name ):
    """Get a repository from the database via name"""
    return trans.sa_session.query( trans.model.Repository ).filter_by( name=name ).one()
def get_changectx_for_changeset( trans, repo, changeset_revision, **kwd ):
    """Retrieve a specified changectx from a repository"""
    for changeset in repo.changelog:
        ctx = repo.changectx( changeset )
        if str( ctx ) == changeset_revision:
            return ctx
    return None
def change_set_is_malicious( trans, id, changeset_revision, **kwd ):
    """Check the malicious flag in repository metadata for a specified change set"""
    repository_metadata = get_repository_metadata_by_changeset_revision( trans, id, changeset_revision )
    if repository_metadata:
        return repository_metadata.malicious
    return False
def get_configured_ui():
    # Configure any desired ui settings.
    _ui = ui.ui()
    # The following will suppress all messages.  This is
    # the same as adding the following setting to the repo
    # hgrc file' [ui] section:
    # quiet = True
    _ui.setconfig( 'ui', 'quiet', True )
    return _ui
def get_user( trans, id ):
    """Get a user from the database by id"""
    return trans.sa_session.query( trans.model.User ).get( trans.security.decode_id( id ) )
def handle_email_alerts( trans, repository ):
    repo_dir = repository.repo_path
    repo = hg.repository( get_configured_ui(), repo_dir )
    smtp_server = trans.app.config.smtp_server
    if smtp_server and repository.email_alerts:
        # Send email alert to users that want them.
        if trans.app.config.email_from is not None:
            email_from = trans.app.config.email_from
        elif trans.request.host.split( ':' )[0] == 'localhost':
            email_from = 'galaxy-no-reply@' + socket.getfqdn()
        else:
            email_from = 'galaxy-no-reply@' + trans.request.host.split( ':' )[0]
        tip_changeset = repo.changelog.tip()
        ctx = repo.changectx( tip_changeset )
        t, tz = ctx.date()
        date = datetime( *time.gmtime( float( t ) - tz )[:6] )
        display_date = date.strftime( "%Y-%m-%d" )
        try:
            username = ctx.user().split()[0]
        except:
            username = ctx.user()
        # Build the email message
        body = string.Template( email_alert_template ) \
            .safe_substitute( host=trans.request.host,
                              repository_name=repository.name,
                              revision='%s:%s' %( str( ctx.rev() ), ctx ),
                              display_date=display_date,
                              description=ctx.description(),
                              username=username )
        frm = email_from
        subject = "Galaxy tool shed repository update alert"
        email_alerts = from_json_string( repository.email_alerts )
        for email in email_alerts:
            to = email.strip()
            # Send it
            try:
                util.send_mail( frm, to, subject, body, trans.app.config )
            except Exception, e:
                log.exception( "An error occurred sending a tool shed repository update alert by email." )
def update_for_browsing( trans, repository, current_working_dir, commit_message='' ):
    # Make a copy of a repository's files for browsing, remove from disk all files that
    # are not tracked, and commit all added, modified or removed files that have not yet
    # been committed.
    repo_dir = repository.repo_path
    repo = hg.repository( get_configured_ui(), repo_dir )
    # The following will delete the disk copy of only the files in the repository.
    #os.system( 'hg update -r null > /dev/null 2>&1' )
    repo.ui.pushbuffer()
    files_to_remove_from_disk = []
    files_to_commit = []
    commands.status( repo.ui, repo, all=True )
    status_and_file_names = repo.ui.popbuffer().strip().split( "\n" )
    if status_and_file_names and status_and_file_names[ 0 ] not in [ '' ]:
        # status_and_file_names looks something like:
        # ['? README', '? tmap_tool/tmap-0.0.9.tar.gz', '? dna_filtering.py', 'C filtering.py', 'C filtering.xml']
        # The codes used to show the status of files are:
        # M = modified
        # A = added
        # R = removed
        # C = clean
        # ! = deleted, but still tracked
        # ? = not tracked
        # I = ignored
        for status_and_file_name in status_and_file_names:
            if status_and_file_name.startswith( '?' ) or status_and_file_name.startswith( 'I' ):
                files_to_remove_from_disk.append( os.path.abspath( os.path.join( repo_dir, status_and_file_name.split()[1] ) ) )
            elif status_and_file_name.startswith( 'M' ) or status_and_file_name.startswith( 'A' ) or status_and_file_name.startswith( 'R' ):
                files_to_commit.append( os.path.abspath( os.path.join( repo_dir, status_and_file_name.split()[1] ) ) )
    # We may have files on disk in the repo directory that aren't being tracked, so they must be removed.
    cmd = 'hg status'
    tmp_name = tempfile.NamedTemporaryFile().name
    tmp_stdout = open( tmp_name, 'wb' )
    os.chdir( repo_dir )
    proc = subprocess.Popen( args=cmd, shell=True, stdout=tmp_stdout.fileno() )
    returncode = proc.wait()
    os.chdir( current_working_dir )
    tmp_stdout.close()
    if returncode == 0:
        for i, line in enumerate( open( tmp_name ) ):
            if line.startswith( '?' ) or line.startswith( 'I' ):
                files_to_remove_from_disk.append( os.path.abspath( os.path.join( repo_dir, line.split()[1] ) ) )
            elif line.startswith( 'M' ) or line.startswith( 'A' ) or line.startswith( 'R' ):
                files_to_commit.append( os.path.abspath( os.path.join( repo_dir, line.split()[1] ) ) )
    for full_path in files_to_remove_from_disk:
        # We'll remove all files that are not tracked or ignored.
        if os.path.isdir( full_path ):
            try:
                os.rmdir( full_path )
            except OSError, e:
                # The directory is not empty
                pass
        elif os.path.isfile( full_path ):
            os.remove( full_path )
            dir = os.path.split( full_path )[0]
            try:
                os.rmdir( dir )
            except OSError, e:
                # The directory is not empty
                pass
    if files_to_commit:
        if not commit_message:
            commit_message = 'Committed changes to: %s' % ', '.join( files_to_commit )
        repo.dirstate.write()
        repo.commit( user=trans.user.username, text=commit_message )
    os.chdir( repo_dir )
    os.system( 'hg update > /dev/null 2>&1' )
    os.chdir( current_working_dir )
def load_tool( trans, config_file ):
    """
    Load a single tool from the file named by `config_file` and return 
    an instance of `Tool`.
    """
    # Parse XML configuration file and get the root element
    tree = util.parse_xml( config_file )
    root = tree.getroot()
    if root.tag == 'tool':
        # Allow specifying a different tool subclass to instantiate
        if root.find( "type" ) is not None:
            type_elem = root.find( "type" )
            module = type_elem.get( 'module', 'galaxy.tools' )
            cls = type_elem.get( 'class' )
            mod = __import__( module, globals(), locals(), [cls])
            ToolClass = getattr( mod, cls )
        elif root.get( 'tool_type', None ) is not None:
            ToolClass = tool_types.get( root.get( 'tool_type' ) )
        else:
            ToolClass = Tool
        return ToolClass( config_file, root, trans.app )
    return None
def build_changeset_revision_select_field( trans, repository, selected_value=None, add_id_to_name=True ):
    """
    Build a SelectField whose options are the changeset_revision
    strings of all downloadable_revisions of the received repository.
    """
    repo = hg.repository( get_configured_ui(), repository.repo_path )
    options = []
    refresh_on_change_values = []
    for repository_metadata in repository.downloadable_revisions:
        changeset_revision = repository_metadata.changeset_revision
        revision_label = get_revision_label( trans, repository, changeset_revision )
        options.append( ( revision_label, changeset_revision ) )
        refresh_on_change_values.append( changeset_revision )
    if add_id_to_name:
        name = 'changeset_revision_%d' % repository.id
    else:
        name = 'changeset_revision'
    select_field = SelectField( name=name,
                                refresh_on_change=True,
                                refresh_on_change_values=refresh_on_change_values )
    for option_tup in options:
        selected = selected_value and option_tup[1] == selected_value
        select_field.add_option( option_tup[0], option_tup[1], selected=selected )
    return select_field
def encode( val ):
    if isinstance( val, dict ):
        value = simplejson.dumps( val )
    else:
        value = val
    a = hmac_new( 'ToolShedAndGalaxyMustHaveThisSameKey', value )
    b = binascii.hexlify( value )
    return "%s:%s" % ( a, b )
def decode( value ):
    # Extract and verify hash
    a, b = value.split( ":" )
    value = binascii.unhexlify( b )
    test = hmac_new( 'ToolShedAndGalaxyMustHaveThisSameKey', value )
    assert a == test
    # Restore from string
    try:
        values = json_fix( simplejson.loads( value ) )
    except Exception, e:
        # We do not have a json string
        values = value
    return values
