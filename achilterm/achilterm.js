achilterm={};
achilterm.Terminal_ctor=function(id,width,height) {
	// 128 random bits, which can't be guessed.
	var sid=Array.from(crypto.getRandomValues(new Uint8Array(16)), function(byte) {
		return byte.toString(16).padStart(2,'0');
	}).join('');
	// The fields sent with every request. c asks for colors, and i names the run
	// of the server this page is talking to.
	var params=new URLSearchParams({s: sid, w: width, h: height, c: 1, i: ''});
	var timeout;
	var error_timeout;
	// typed or pasted, and not yet sent
	var keybuf='';
	var sending=0;
	var retrying=false;
	var rmax=1;

	var div=document.getElementById(id);
	var dstat=document.createElement('pre');
	var sled=document.createElement('span');
	var opt_color=document.createElement('a');
	var opt_paste=document.createElement('a');
	var sdebug=document.createElement('span');
	var dterm=document.createElement('div');

	function debug(s) {
		sdebug.innerHTML=s;
	}
	function error() {
		sled.className='off';
		debug(`Connection lost timeout ts:${(new Date).getTime()}`);
	}
	function opt_add(opt,name) {
		opt.className='off';
		opt.innerHTML=` ${name} `;
		dstat.appendChild(opt);
		dstat.appendChild(document.createTextNode(' '));
	}
	function do_color(event) {
		var o=opt_color.className=(opt_color.className=='off')?'on':'off';
		if(o=='on')
			params.set('c',1);
		else
			params.delete('c');
		debug(`Color ${opt_color.className}`);
	}
	function do_paste(event) {
		// Browsers only offer the clipboard to pages served over HTTPS or from localhost.
		if (!navigator.clipboard || !navigator.clipboard.readText) {
			debug('Paste needs HTTPS or localhost');
			return;
		}
		navigator.clipboard.readText().then(function(text) {
			if (text) {
				// Send line breaks as the Enter key does.
				queue(text.replace(/\r?\n/g, '\r'));
				debug('Pasted');
			}
		}, function() {
			debug('Clipboard access was denied');
		});
	}
	function update() {
//		debug("ts: "+((new Date).getTime())+" rmax:"+rmax);
		if(sending==0) {
			sending=1;
			sled.className='on';
			var r=new XMLHttpRequest();
			params.set('k',keybuf);
			keybuf='';
			r.open("POST","u",true);
			// Shows the server that this request comes from its own page.
			r.setRequestHeader('X-Achilterm','1');
			r.onreadystatechange = function () {
//				debug("xhr:"+((new Date).getTime())+" state:"+r.readyState+" status:"+r.status+" statusText:"+r.statusText);
				if (r.readyState==4) {
					window.clearTimeout(error_timeout);
					if(r.status==200) {
						if(retrying) {
							retrying=false;
							debug('');
						}
						params.set('i',r.getResponseHeader('X-Achilterm-Instance') || '');
						var de=r.responseXML.documentElement;
						if(de.tagName=="pre") {
                    					dterm.innerHTML = r.responseText;
							rmax=100;
						} else {
							rmax*=2;
							if(rmax>2000)
								rmax=2000;
						}
						sending=0;
						sled.className='off';
						timeout=window.setTimeout(update,rmax);
					} else if(r.status==0 || r.status>=500) {
						// The server or the connection may only be away briefly.
						retrying=true;
						sending=0;
						sled.className='off';
						debug(`Connection lost, retrying. Status:${r.status}`);
						timeout=window.setTimeout(update,2000);
					} else {
						// The server refused this page, and would again.
						sled.className='off';
						sdebug.textContent=r.responseText || `Connection error status:${r.status}`;
					}
				}
			}
			error_timeout=window.setTimeout(error,5000);
			r.send(params);
		}
	}
	function queue(s) {
		keybuf+=s;
		if(sending==0) {
			window.clearTimeout(timeout);
			timeout=window.setTimeout(update,1);
		}
	}
	// Keys that don't type a character, and what a Linux console sends for them.
	var special_keys={
		'Backspace':'\x7f', 'Tab':'\t', 'Escape':'\x1b',
		'ArrowUp':'\x1b[A', 'ArrowDown':'\x1b[B', 'ArrowRight':'\x1b[C', 'ArrowLeft':'\x1b[D',
		'Home':'\x1b[1~', 'Insert':'\x1b[2~', 'Delete':'\x1b[3~', 'End':'\x1b[4~',
		'PageUp':'\x1b[5~', 'PageDown':'\x1b[6~',
		'F1':'\x1b[[A', 'F2':'\x1b[[B', 'F3':'\x1b[[C', 'F4':'\x1b[[D', 'F5':'\x1b[[E',
		'F6':'\x1b[17~', 'F7':'\x1b[18~', 'F8':'\x1b[19~', 'F9':'\x1b[20~', 'F10':'\x1b[21~',
		'F11':'\x1b[23~', 'F12':'\x1b[24~'
	};
	// Ctrl with these characters, in addition to Ctrl with a letter.
	var control_keys={'@':0, '[':27, '\\':28, ']':29, '6':30, '^':30, '-':31, '_':31};
	function is_altgr(ev) {
		// AltGr types characters, though some systems also report Ctrl and Alt.
		return ev.getModifierState('AltGraph');
	}
	function letter(ev) {
		if(/^[a-z]$/i.test(ev.key))
			return ev.key.toLowerCase();
		// On other layouts, such as Cyrillic, use the key's position.
		if(ev.key.length==1 && ev.key.charCodeAt(0)>127 && /^Key[A-Z]$/.test(ev.code))
			return ev.code.charAt(3).toLowerCase();
		return '';
	}
	function send_key(ev,k) {
		ev.preventDefault();
		ev.stopPropagation();
		queue(k);
	}
	// Special keys and Ctrl or Alt combinations. Anything not sent here is left
	// to the browser, and characters then arrive as keypress events.
	function keydown(ev) {
		if(ev.metaKey || ev.isComposing || is_altgr(ev))
			return;
		var k;
		if(ev.ctrlKey && ev.altKey) {
			return;
		} else if(ev.ctrlKey) {
			if(letter(ev))
				k=String.fromCharCode(letter(ev).charCodeAt(0)-96); // Ctrl-A..Z
			else if(control_keys.hasOwnProperty(ev.key))
				k=String.fromCharCode(control_keys[ev.key]);
		} else if(ev.altKey) {
			if(letter(ev))
				k=`\x1b${letter(ev)}`;
		} else if(special_keys.hasOwnProperty(ev.key)) {
			k=special_keys[ev.key];
		}
		if(k!==undefined)
			send_key(ev,k);
	}
	function keypress(ev) {
		if(ev.metaKey || ((ev.ctrlKey || ev.altKey) && !is_altgr(ev)))
			return;
		if(ev.key=='Enter')
			send_key(ev,'\r');
		else if(ev.charCode)
			send_key(ev,String.fromCodePoint(ev.charCode));
	}
	function init() {
		sled.appendChild(document.createTextNode('\xb7'));
		sled.className='off';
		dstat.appendChild(sled);
		dstat.appendChild(document.createTextNode(' '));
		opt_add(opt_color,'Colors');
		opt_color.className='on';
		opt_add(opt_paste,'Paste');
		dstat.appendChild(sdebug);
		dstat.className='stat';
		div.appendChild(dstat);
		div.appendChild(dterm);
		opt_color.addEventListener('click',do_color,true);
		opt_paste.addEventListener('click',do_paste,true);
		document.onkeypress=keypress;
		document.onkeydown=keydown;
		timeout=window.setTimeout(update,100);
	}
	init();
}
achilterm.Terminal=function(id,width,height) {
	return new this.Terminal_ctor(id,width,height);
}
