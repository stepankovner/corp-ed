<?php
// Настройки стенда kronto поверх LocalSettings.php установщика. Пароли
// и учётки стенда выдуманные (127.0.0.1).

$wgEnableUploads = true;
$wgFileExtensions = array_merge( $wgFileExtensions, [ 'txt', 'pdf', 'docx' ] );
$wgGroupPermissions['*']['createaccount'] = false;
$wgGroupPermissions['*']['edit'] = false;
// Пространство «Кадры» — на обеих вики; на закрытой его закрывает Lockdown.
$wgExtraNamespaces[3000] = 'Кадры';
$wgExtraNamespaces[3001] = 'Обсуждение_кадров';

if ( getenv( 'WIKI_PRIVATE' ) === '1' ) {
	// Без входа не читается ничего; из вошедших — только группа staff
	// (и администраторы).
	$wgGroupPermissions['*']['read'] = false;
	$wgGroupPermissions['user']['read'] = false;
	$wgGroupPermissions['staff']['read'] = true;
	$wgGroupPermissions['sysop']['read'] = true;
	$wgGroupPermissions['hr']['read'] = true;
	// Файлы — через img_auth.php: он проверяет право чтения по сессии.
	$wgUploadPath = "$wgScriptPath/img_auth.php";
	// Права на пространство имён, которых API не показывает.
	wfLoadExtension( 'Lockdown' );
	$wgNamespacePermissionLockdown[3000]['*'] = [ 'hr', 'sysop' ];
}
