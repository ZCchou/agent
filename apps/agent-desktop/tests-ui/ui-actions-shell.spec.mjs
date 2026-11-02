import { expect, test } from '@playwright/test';
import { installTauriMock } from './tauri-mock.mjs';

async function systemPage(page, name) {
  const toggle=page.getByRole('button',{name:'System features'});
  if(await toggle.getAttribute('aria-expanded')==='false') await toggle.click();
  await page.getByRole('navigation').getByRole('button',{name,exact:true}).click();
}
async function setup(page, overrides={}) {
  await installTauriMock(page, overrides);
  await page.addInitScript(() => localStorage.setItem('agent:update-auto-check','false'));
}
const mutations=[{kind:'create_text',path:'a.txt',content:'A'},{kind:'create_text',path:'b.txt',content:'B'}];
const change={id:'audit-change',operation_id:'op-audit',run_id:'run-audit',conversation_id:'work-1',project_id:'project-1',title:'Audit changes',
  risk:'medium',approval_mode:'human',mutations,status:'waiting_user',expires_at_epoch_ms:9999999999999,recovery_point_id:null,undo_state:'unavailable',
  plan:{plan_id:'audit-plan',plan_hash:'audit-hash',policy_version:1,root_generation:1,effective_risk:'medium',steps:mutations.map((mutation,i)=>({mutation,operation_id:`file-${i}`,depends_on:[],preconditions:{}}))}};

test('approval selection and actions stay on Approvals until explicit navigation',async({page})=>{
  await setup(page,{list_agent_change_sets:[change],resolve_agent_change_set:null});
  await page.goto('http://127.0.0.1:5173'); await systemPage(page,'Approvals');
  const card=page.locator('.approvals-page .change-set-card');
  await card.getByRole('checkbox',{name:'Select b.txt'}).uncheck();
  await expect(page.locator('.approvals-page')).toBeVisible();
  await card.getByRole('button',{name:'Approve 1 selected'}).click();
  await expect(page.locator('.approvals-page')).toBeVisible();
  await page.getByRole('button',{name:'Open conversation',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'Ask agent...'})).toBeVisible();
});

test('Undo confirmation from Approvals remains open and cancel leaves the page intact',async({page})=>{
  await setup(page,{list_agent_change_sets:[{...change,status:'completed',recovery_point_id:'recovery-audit',undo_state:'available'}]});
  await page.goto('http://127.0.0.1:5173'); await systemPage(page,'Approvals');
  await page.getByRole('button',{name:'Undo changes',exact:true}).click();
  await expect(page.getByRole('alertdialog')).toBeVisible();
  await page.getByRole('alertdialog').getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(page.locator('.approvals-page')).toBeVisible();
});

test('global instructions can retry a failed initial load without leaving the page',async({page})=>{
  await setup(page);
  await page.addInitScript(()=>{
    const invoke=window.__TAURI_INTERNALS__.invoke;let attempts=0;
    window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
      if(command==='get_global_instructions' && ++attempts===1) throw new Error('synthetic offline');
      return invoke(command,args);
    };
  });
  await page.goto('http://127.0.0.1:5173'); await systemPage(page,'Global behavior instructions');
  await expect(page.getByRole('textbox',{name:'Global behavior instructions'})).toBeDisabled();
  await page.getByRole('button',{name:'Retry',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'Global behavior instructions'})).toBeEnabled();
});

for(const control of ['Minimize','Maximize or restore','Close']) {
  test(`window ${control} reports a native failure and can be retried`,async({page})=>{
    await setup(page);
    await page.addInitScript((name)=>{
      const invoke=window.__TAURI_INTERNALS__.invoke;let fail=true;
      const target=name==='Minimize'?'minimize':name==='Close'?'close':'toggle_maximize';
      window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
        if(command===`plugin:window|${target}`){if(fail){fail=false;throw new Error('synthetic window failure');}return;}
        return invoke(command,args);
      };
    },control);
    await page.goto('http://127.0.0.1:5173');
    await page.getByRole('button',{name:control,exact:true}).click();
    await expect(page.locator('.window-control-error')).toBeVisible();
    await page.getByRole('button',{name:control,exact:true}).click();
    await expect(page.locator('.window-control-error')).toHaveCount(0);
  });
}

test('a skill save stays committed when its subsequent list refresh fails',async({page})=>{
  await setup(page);
  await page.addInitScript(()=>{
    const invoke=window.__TAURI_INTERNALS__.invoke;let saved=false;let failed=false;
    const skill={id:'audit-skill',name:'audit-skill',description:'Audit instructions',content:'Saved teaching content',revision:'b'.repeat(64),path:'C:\\synthetic\\audit-skill',enabled:true,implicit:true,error:null};
    window.skillSaves=0;
    window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
      if(command==='list_skills'){if(saved&&!failed){failed=true;throw new Error('synthetic refresh failure');}return saved?[skill]:[];}
      if(command==='save_skill'){window.skillSaves++;saved=true;return skill;}
      return invoke(command,args);
    };
  });
  await page.goto('http://127.0.0.1:5173');await systemPage(page,'Skills');
  await page.getByRole('button',{name:'Create skill',exact:true}).click();
  await page.getByLabel('Skill name',{exact:true}).fill('audit-skill');
  await page.getByLabel('When to use',{exact:true}).fill('Audit instructions');
  await page.getByLabel('Instructions (Markdown)',{exact:true}).fill('Saved teaching content');
  await page.getByRole('button',{name:'Save skill',exact:true}).click();
  await expect(page.locator('.skill-editor')).toHaveCount(0);
  await expect(page.locator('.skill-source')).toContainText('Saved teaching content');
  expect(await page.evaluate(()=>window.skillSaves)).toBe(1);
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect(page.getByRole('button',{name:'Edit skill',exact:true})).toBeVisible();
});

test('a late offline status cannot overwrite a newer successful health refresh',async({page})=>{
  await setup(page);
  await page.addInitScript(()=>{
    const invoke=window.__TAURI_INTERNALS__.invoke;
    const interval=window.setInterval.bind(window);
    window.setInterval=(callback,delay,...args)=>{if(delay===5000)window.coreTick=callback;return interval(callback,delay,...args);};
    window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
      if(command==='agent_core_status'&&window.delayNextStatus){window.delayNextStatus=false;return new Promise(resolve=>{window.oldCoreStatus=()=>resolve({running:false,healthy:false});});}
      return invoke(command,args);
    };
  });
  await page.goto('http://127.0.0.1:5173');await systemPage(page,'Diagnostics');
  await expect(page.locator('.diagnostics-status')).toContainText('Online');
  await page.evaluate(()=>{window.delayNextStatus=true;});
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.waitForFunction(()=>typeof window.oldCoreStatus==='function');
  await page.evaluate(()=>window.coreTick());
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.evaluate(()=>window.oldCoreStatus());
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await expect(page.locator('.diagnostics-status')).toContainText('Online');
});

test('resizing then collapsing retains the saved width after reload',async({page})=>{
  await setup(page);
  await page.addInitScript(()=>{
    const invoke=window.__TAURI_INTERNALS__.invoke;
    let sidebar=JSON.parse(localStorage.getItem('audit-sidebar')||'null');
    window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
      if(command==='get_app_settings'){const settings=await invoke(command,args);return {...settings,sidebar:sidebar??settings.sidebar};}
      if(command==='update_sidebar_width'||command==='update_sidebar_preferences'){
        const current=await invoke('get_app_settings');sidebar=command==='update_sidebar_width'?{...(sidebar??current.sidebar),width_px:args.widthPx}:args.preferences;
        localStorage.setItem('audit-sidebar',JSON.stringify(sidebar));return {...current,sidebar};
      }
      return invoke(command,args);
    };
  });
  await page.goto('http://127.0.0.1:5173');
  const separator=page.getByRole('separator',{name:'Resize sidebar'});await expect(separator).toHaveAttribute('aria-valuenow','280');
  await separator.focus();for(let i=0;i<10;i++)await separator.press('ArrowRight');
  await expect(separator).toHaveAttribute('aria-valuenow','360');
  await page.getByRole('button',{name:'Projects',exact:true}).click();
  await page.reload();await expect(separator).toHaveAttribute('aria-valuenow','360');
});

test('the sidebar New Work action remains clickable beside the project name',async({page})=>{
  await setup(page);await page.setViewportSize({width:1000,height:680});
  await page.goto('http://127.0.0.1:5173');
  await page.getByTitle('Open projects',{exact:true}).click();
  await page.mouse.move(0,0);
  await page.getByTitle('New work',{exact:true}).click();
  await expect(page.getByRole('textbox',{name:'Ask agent...'})).toBeVisible();
});

test('reset while an older sidebar resize save is pending persists the reset',async({page})=>{
  await setup(page);
  await page.addInitScript(()=>{
    const invoke=window.__TAURI_INTERNALS__.invoke;let sidebar;let first=true;window.resizeCommits=0;
    window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
      if(command==='get_app_settings'){const s=await invoke(command,args);return {...s,sidebar:sidebar??s.sidebar};}
      if(command==='update_sidebar_width'){
        if(first){first=false;await new Promise(resolve=>{window.finishResize=resolve;});}
        const s=await invoke('get_app_settings');sidebar={...(sidebar??s.sidebar),width_px:args.widthPx};
        window.savedWidth=args.widthPx;window.resizeCommits++;return {...s,sidebar};
      }
      return invoke(command,args);
    };
  });
  await page.goto('http://127.0.0.1:5173');
  const separator=page.getByRole('separator',{name:'Resize sidebar'});
  await expect(separator).toHaveAttribute('aria-valuenow','280');await separator.focus();await separator.press('ArrowRight');
  await page.waitForFunction(()=>typeof window.finishResize==='function');
  await separator.dblclick();await expect(separator).toHaveAttribute('aria-valuenow','280');
  await page.evaluate(()=>window.finishResize());
  await expect.poll(()=>page.evaluate(()=>window.resizeCommits)).toBeGreaterThan(0);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await expect(separator).toHaveAttribute('aria-valuenow','280');
  expect(await page.evaluate(()=>window.savedWidth)).toBe(280);
});

test('a failed older collapse save cannot undo a newer successful section choice',async({page})=>{
  await setup(page);
  await page.addInitScript(()=>{
    const invoke=window.__TAURI_INTERNALS__.invoke;let sidebar;window.collapseWrites=0;
    window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
      if(command==='get_app_settings'){const s=await invoke(command,args);return {...s,sidebar:sidebar??s.sidebar};}
      if(command==='update_sidebar_preferences'){
        window.collapseWrites++;
        if(window.collapseWrites===1)await new Promise((_,reject)=>{window.failCollapse=()=>reject('synthetic save failure');});
        const s=await invoke('get_app_settings');sidebar=args.preferences;return {...s,sidebar};
      }
      return invoke(command,args);
    };
  });
  await page.goto('http://127.0.0.1:5173');
  const projects=page.getByRole('button',{name:'Projects',exact:true});const chats=page.getByRole('button',{name:'Chats',exact:true});
  await expect(projects).toHaveAttribute('aria-expanded','true');await projects.click();
  await page.waitForFunction(()=>typeof window.failCollapse==='function');await chats.click();
  await page.evaluate(()=>window.failCollapse());await expect.poll(()=>page.evaluate(()=>window.collapseWrites)).toBe(2);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await expect(projects).toHaveAttribute('aria-expanded','false');await expect(chats).toHaveAttribute('aria-expanded','false');
});
