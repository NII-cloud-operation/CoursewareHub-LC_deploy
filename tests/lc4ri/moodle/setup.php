<?php
// Register an actual LTI 1.3 tool and course using Moodle's application APIs.
define('CLI_SCRIPT', true);
require('/var/www/html/config.php');
require_once($CFG->dirroot . '/course/lib.php');
require_once($CFG->dirroot . '/course/modlib.php');
require_once($CFG->dirroot . '/user/lib.php');
require_once($CFG->dirroot . '/mod/lti/locallib.php');
$USER = get_admin();
if (($argv[1] ?? '') === 'public-key') {
    $type = $DB->get_record('lti_types', ['id' => (int)$argv[2]], '*', MUST_EXIST);
    lti_update_type($type, (object)['lti_publickey' => stream_get_contents(STDIN)]);
    echo "Tool public key registered\n";
    exit;
}
$toolurl = 'https://coursewarehub.test/php/lti/service.php';
$typeid = lti_add_type((object)[
    'name' => 'CoursewareHub', 'baseurl' => $toolurl,
    'state' => LTI_TOOL_STATE_CONFIGURED, 'ltiversion' => LTI_VERSION_1P3,
    'coursevisible' => LTI_COURSEVISIBLE_PRECONFIGURED,
], (object)[
    'lti_toolurl' => $toolurl,
    'lti_initiatelogin' => 'https://coursewarehub.test/php/lti/login.php',
    'lti_redirectionuris' => $toolurl,
    'lti_keytype' => LTI_RSA_KEY,
    'lti_sendname' => LTI_SETTING_ALWAYS,
    'lti_sendemailaddr' => LTI_SETTING_ALWAYS,
    'lti_acceptgrades' => LTI_SETTING_NEVER,
    'lti_launchcontainer' => LTI_LAUNCH_CONTAINER_REPLACE_MOODLE_WINDOW,
]);
$course = create_course((object)[
    'fullname' => 'CoursewareHub LTI E2E', 'shortname' => 'cwh-lti-e2e',
    'category' => 1, 'format' => 'topics', 'visible' => 1,
]);
$enrol = enrol_get_plugin('manual');
$instance = $DB->get_record('enrol', ['courseid' => $course->id, 'enrol' => 'manual']);
if (!$instance) {
    $instanceid = $enrol->add_instance($course);
    $instance = $DB->get_record('enrol', ['id' => $instanceid], '*', MUST_EXIST);
}
foreach (['teacher' => ['editingteacher', 'ciadmin@example.test'],
          'student' => ['student', 'cistudent@example.test']] as $name => [$role, $email]) {
    $userid = user_create_user((object)[
        'username' => $name, 'password' => getenv('CWH_MOODLE_PASSWORD'),
        'firstname' => 'CI', 'lastname' => ucfirst($name), 'email' => $email,
        'auth' => 'manual', 'confirmed' => 1, 'mnethostid' => $CFG->mnet_localhost_id,
    ]);
    $roleid = $DB->get_field('role', 'id', ['shortname' => $role], MUST_EXIST);
    $enrol->enrol_user($instance, $userid, $roleid);
}
$module = add_moduleinfo((object)[
    'modulename' => 'lti', 'module' => $DB->get_field('modules', 'id', ['name' => 'lti'], MUST_EXIST),
    'name' => 'Open CoursewareHub', 'typeid' => $typeid, 'toolurl' => $toolurl,
    'intro' => '', 'introformat' => FORMAT_HTML, 'section' => 0, 'visible' => 1,
    'showdescription' => 0, 'grade' => 0,
    'instructorchoicesendname' => LTI_SETTING_ALWAYS,
    'instructorchoicesendemailaddr' => LTI_SETTING_ALWAYS,
    'instructorchoiceacceptgrades' => LTI_SETTING_NEVER,
    'launchcontainer' => LTI_LAUNCH_CONTAINER_REPLACE_MOODLE_WINDOW,
], $course);
$type = $DB->get_record('lti_types', ['id' => $typeid], '*', MUST_EXIST);
echo json_encode(['client_id' => $type->clientid, 'deployment_id' => (string)$typeid,
                  'course_id' => $course->id, 'activity_id' => $module->coursemodule]) . "\n";
