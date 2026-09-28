using System.Collections;
using System.Collections.Generic;
using Unity.VisualScripting;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.EventSystems;

public class UIManager : MonoBehaviour
{
    public GameObject pointSourcePrefab, listenPlanePrefab;

    public List<GameObject> selected;

    public List<GameObject> clipboard;

    //indicate if you are dragging something
    public bool dragged;
    AudioManager manager;

    CameraControl cameraControl;

    Vector3 startingMousePosition;

    Hierarchy hierarchy;
    Inspector inspector;

    GraphicRaycaster m_Raycaster;
    PointerEventData m_PointerEventData;
    EventSystem m_EventSystem;

    int nameNumber=1;
    // Start is called before the first frame update
    void Start()
    {

        //Fetch the Raycaster from the GameObject (the Canvas)
        m_Raycaster = GetComponent<GraphicRaycaster>();
        //Fetch the Event System from the Scene
        m_EventSystem = GetComponent<EventSystem>();


        selected= new List<GameObject>();
     
        manager = FindObjectOfType<AudioManager>();

        cameraControl=FindObjectOfType<CameraControl>();
        hierarchy=FindObjectOfType<Hierarchy>();
        inspector=FindObjectOfType<Inspector>();
    }

    // Update is called once per frame
    void Update()
    {
        if (Input.GetMouseButtonDown(0)){
            startingMousePosition=Input.mousePosition;            
            Ray ray = Camera.main.ScreenPointToRay(Input.mousePosition);
            RaycastHit[] hits=Physics.RaycastAll(ray);            
            if (hits.Length>0)
            {
                bool found=false;
                foreach (RaycastHit hit in hits){
                    if (hit.collider.GetComponent<ManipulateAxis>()!=null){
                        hit.collider.GetComponent<ManipulateAxis>().activateDragging=true;
                        found=true;
                        break;
                    }
                }
                if (!found){
                    //if the mouse is not inside the visualizer zone, we are not dragging the camera                    
                    if (Input.mousePosition.x>130 && Input.mousePosition.y>30 && Input.mousePosition.y<Screen.height-30){
                        cameraControl.activateDragging=true;
                    }
                }
            }
            dragged = false;
        }
        if (Input.GetMouseButton(0)){
            if (Input.mousePosition!=startingMousePosition){
                dragged=true;
            }
        }
        if (Input.GetMouseButtonUp(0)){
            //Set up the new Pointer Event
            m_PointerEventData = new PointerEventData(m_EventSystem);
            //Set the Pointer Event Position to that of the mouse position
            m_PointerEventData.position = Input.mousePosition;

            //Create a list of Raycast Results
            List<RaycastResult> results = new List<RaycastResult>();

            //Raycast using the Graphics Raycaster and mouse click position
            m_Raycaster.Raycast(m_PointerEventData, results);

            //For every result returned, output the name of the GameObject on the Canvas hit by the Ray
            bool found =false;
            foreach (RaycastResult result in results)
            {
                if(result.gameObject.CompareTag("VisualizerZone")){
                    found=true;
                    break;
                }
            }
            if (found)
            {                                     
                SelectionManagement();
            }
        }
        if (Input.GetButton("Delete")){
            RemoveSelected();
        }
        
        if (Input.GetKey(KeyCode.LeftControl) || Input.GetKey(KeyCode.RightControl) || Input.GetKey(KeyCode.LeftCommand) || Input.GetKey(KeyCode.RightCommand)){        
            //if CTRL+C or CMD+C is pressed, copy selected items
            if (Input.GetKeyDown(KeyCode.C)){
                CopySelected();
            }        
            //if CTRL+V or CMD+V is pressed, paste copied items
            if (Input.GetKeyDown(KeyCode.V)){
                Paste();
            }
        }
    }

    public void DeselectAll(){
        foreach(GameObject obj in selected){
            Selectable sel = obj.GetComponent<Selectable>();
            sel.Select(false);
        }
        selected = new List<GameObject>();
        inspector.DeselectAll();
    }

    public void Deselect(GameObject go){
        Selectable sel = go.GetComponent<Selectable>();
        sel.Select(false);
        selected.Remove(go);
        inspector.Deselect(sel);
    }

    public void AddToSelection(GameObject go){
        Selectable sel = go.GetComponent<Selectable>();
        sel.Select(true);
        selected.Add(go);
        inspector.AddToSelection(sel);
    }

    void SelectionManagement(){
        if (dragged){
            return;
        }
        bool multipleSelection = Input.GetAxis("MultipleSelection")==1;            

        RaycastHit hit;
        Ray ray = Camera.main.ScreenPointToRay(Input.mousePosition);
        if (Physics.Raycast(ray, out hit))
        {
            Selectable sel = hit.collider.GetComponent<Selectable>();                
            if (sel == null)
            {
                if (!multipleSelection)
                {
                    DeselectAll();
                }
                return;
            }
            if (selected.Contains(sel.gameObject)){
                if (!multipleSelection){
                    DeselectAll();
                    AddToSelection(sel.gameObject);
                }
                else{
                    Deselect(sel.gameObject);
                }
            }
            else{
                if (!multipleSelection){
                    DeselectAll();
                }
                AddToSelection(sel.gameObject);
            }
        }    
    }

    public void MoveSelected (Vector3 move){
        foreach(GameObject go in selected){
            go.transform.position+=move;
        }
    }

    public void AddPointSource(AudioFixture type){
        DeselectAll();
        GameObject source = Instantiate(pointSourcePrefab);
        source.name="s"+nameNumber;
        nameNumber++;
        source.GetComponent<CustomAudioSource>().audioFixture=type;
        manager.audioSources.Add(source.GetComponent<CustomAudioSource>());
        hierarchy.AddItem(source);
        AddToSelection(source);
    }
    public void RemoveSelected(){
        foreach(GameObject go in selected){
            hierarchy.RemoveItem(go);
            if (go.GetComponent<CustomAudioSource>()!=null){
                manager.audioSources.Remove(go.GetComponent<CustomAudioSource>());                
            }
            else if (go.GetComponent<Array>()!=null){
                manager.audioSources.RemoveAll(x => x.parentArray==go.GetComponent<Array>());
            }
            Destroy(go);
        }
        DeselectAll();
    }

    public void CopySelected(){
        clipboard = new List<GameObject>();
        foreach(GameObject go in selected){
            clipboard.Add(go);
        }
    }

    public void Paste() {
        DeselectAll();
        foreach(GameObject go in clipboard){
            GameObject newGo = Instantiate(go);
            hierarchy.AddItem(newGo);
            if (newGo.GetComponent<CustomAudioSource>()!=null){
                manager.audioSources.Add(newGo.GetComponent<CustomAudioSource>());
                newGo.GetComponent<CustomAudioSource>().isSolo=false;
                newGo.GetComponent<CustomAudioSource>().muted=false;
            }
            else{
                newGo.GetComponent<Array>().isSolo=false;
                newGo.GetComponent<Array>().muted=false;
            }
            newGo.name="s"+nameNumber;
            nameNumber++;
            AddToSelection(newGo);
        }        
    }
}
